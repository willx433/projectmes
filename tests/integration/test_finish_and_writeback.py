"""Integration tests for P3-08/09/10: app/domain/failures.py,
app/domain/sessions.py, app/outbox/payloads.py, app/api/operations.py.

Same pattern as tests/integration/test_scan.py (a standalone FastAPI app
against a fresh sqlite engine, domain-level seeding that bypasses
app.sync/app.domain.workorders) plus tests/integration/test_outbox.py's
fake-JB2 bridge for the CR-010 drain check.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.auth import router as auth_router
from app.api.operations import router as operations_router
from app.api.scan import router as scan_router
from app.auth import service
from app.config import config as real_config
from app.db import get_session
from app.domain import failures, statemachine
from app.domain import sessions as sessions_domain
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    BoxAssignment,
    BuildBox,
    Event,
    ScrapEvent,
    SessionPause,
    StepExecution,
    WorkSession,
)
from app.domain.models_jb2 import (
    Base,
    JB2Employee,
    JB2OrderLineItem,
    JB2OrderMaterial,
    JB2OrderRouting,
    JB2Outbox,
    MappingException,
)
from app.domain.models_library import FailureCode, Product
from app.jb2.client import Jb2Client

# Registers the full model graph on Base.metadata (work_orders etc.) --
# importing app.main also pulls in every other Phase-3 router; harmless,
# our test app below only mounts the three routers it actually needs.
from app.main import app as _main_app  # noqa: F401
from app.outbox import drainer, writer
from tests.fake_jb2 import create_fake_jb2, seed_state

TEST_SECRET = "unit-test-secret-key-not-for-prod"


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    original_type = Event.__table__.c.id.type
    Event.__table__.c.id.type = Integer()
    try:
        Base.metadata.create_all(eng)
    finally:
        Event.__table__.c.id.type = original_type
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


def _build_app():
    test_config = dataclasses.replace(real_config, mes_secret_key=TEST_SECRET)
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(scan_router)
    app.include_router(operations_router)
    return app, test_config


@pytest.fixture
def app_and_config():
    return _build_app()


@pytest.fixture
def client(engine, app_and_config, monkeypatch):
    app, test_config = app_and_config
    monkeypatch.setattr("app.api.auth.config", test_config)
    monkeypatch.setattr("app.api.scan.config", test_config)
    monkeypatch.setattr("app.auth.deps.config", test_config)
    monkeypatch.setattr("app.api.operations.config", test_config)

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


# -- seeding helpers -----------------------------------------------------------


def _mirror_common() -> dict:
    return {"content_hash": "h", "jb2_last_modified": None, "synced_at": datetime.now(timezone.utc)}


def _seed_work_order(
    db_session: Session, *, qty: int = 1, op_work_centers: tuple[str, ...] = ("CNC1", "ASSY1"),
    job_number: str = "10008-01",
) -> tuple[WorkOrder, list[PlanOperation], list[Unit]]:
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=qty,
        payload={"jobNumber": job_number, "orderNumber": "10008"}, **_mirror_common(),
    )
    db_session.add(line_item)
    db_session.flush()

    product = Product(id=uuid.uuid4(), name="Apollo", variant_schema={}, active=True)
    db_session.add(product)
    db_session.flush()

    work_order = WorkOrder(
        id=uuid.uuid4(), jb2_line_item_id=line_item.id, product_id=product.id,
        qty=qty, status="in_progress",
    )
    db_session.add(work_order)
    db_session.flush()

    plan_ops = []
    for seq, wc in enumerate(op_work_centers, start=1):
        routing = JB2OrderRouting(
            id=uuid.uuid4(), jb2_id=f"routing-{work_order.id}-{seq}",
            jb2_line_item_id=line_item.id, seq=seq, operation_code=f"OP{seq * 10}",
            description=f"Op {seq}", work_center_code=wc, payload={}, **_mirror_common(),
        )
        db_session.add(routing)
        db_session.flush()

        plan_op = PlanOperation(
            id=uuid.uuid4(), work_order_id=work_order.id, seq=seq,
            jb2_routing_id=routing.id, operation_code=routing.operation_code,
            title=f"Op {seq} ({wc})", frozen_content={"steps": [{"seq": 1, "title": "Step 1"}]},
            status="pending",
        )
        db_session.add(plan_op)
        plan_ops.append(plan_op)
    db_session.flush()

    units = []
    for unit_no in range(1, qty + 1):
        unit = Unit(
            id=uuid.uuid4(), work_order_id=work_order.id, unit_no=unit_no,
            status="queued", first_pass=True, rework_count=0,
        )
        db_session.add(unit)
        units.append(unit)
    db_session.flush()

    return work_order, plan_ops, units


def _bind_box(db_session: Session, unit: Unit, *, payload: str | None = None) -> BuildBox:
    box = BuildBox(
        id=uuid.uuid4(), qr_payload=payload or f"BOX:{uuid.uuid4()}", current_unit_id=unit.id,
    )
    db_session.add(box)
    db_session.flush()
    db_session.add(BoxAssignment(id=uuid.uuid4(), box_id=box.id, unit_id=unit.id))
    db_session.flush()
    return box


def _make_station(db_session, *, name="Station", work_center_code="CNC1"):
    station, token = service.create_station(
        db_session, name=name, work_center_code=work_center_code,
    )
    db_session.commit()
    return station, token


def _make_operator(db_session, *, name="Alice", roles=None):
    operator = service.create_operator(db_session, display_name=name, roles=roles or ["operator"])
    db_session.commit()
    return operator


def _make_operator_with_employee(db_session, *, name="Alice", roles=None, employee_code="42"):
    jb2_emp = JB2Employee(
        id=uuid.uuid4(), jb2_id=f"emp-{uuid.uuid4()}", employee_code=employee_code,
        name=name, active=True, payload={}, **_mirror_common(),
    )
    db_session.add(jb2_emp)
    db_session.flush()
    operator = service.create_operator(
        db_session, display_name=name, roles=roles or ["operator"], jb2_employee_id=jb2_emp.id,
    )
    db_session.commit()
    return operator


def _badge_in(client, *, badge_qr: str, station_token: str):
    resp = client.post(
        "/auth/badge", json={"payload": badge_qr}, headers={"X-Station-Token": station_token}
    )
    assert resp.status_code == 200, resp.text
    return resp


def _scan(client, payload: str, token: str, **extra):
    return client.post(
        "/scan", json={"payload": payload, **extra}, headers={"X-Station-Token": token}
    )


def _open_session_for(db_session: Session, unit: Unit) -> WorkSession:
    return db_session.execute(
        select(WorkSession).where(WorkSession.unit_id == unit.id, WorkSession.ended_at.is_(None))
    ).scalars().one()


def _mark_step_done(db_session: Session, unit: Unit, plan_op: PlanOperation, step_seq: int = 1):
    # ponytail: `superseded` must be set explicitly -- models_floor.py's
    # `server_default="false"` renders as a quoted SQL string literal
    # (`DEFAULT 'false'`), which Postgres coerces to boolean false but
    # SQLite's NUMERIC-affinity Boolean column stores/reads back as a
    # truthy non-empty string when the ORM insert omits the column. A
    # pre-existing sqlite-test-only quirk (see task report), not something
    # this task's files caused -- worked around here by always being explicit.
    step_exec = StepExecution(
        plan_operation_id=plan_op.id, unit_id=unit.id, step_seq=step_seq, status="done",
        started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
        superseded=False,
    )
    db_session.add(step_exec)
    db_session.commit()
    return step_exec


def _force_not_confirmed(db_session: Session, ws: WorkSession) -> None:
    """Same sqlite server_default quirk as `_mark_step_done` -- WorkSession
    .lead_confirmed also reads back truthy on sqlite unless set explicitly.
    `statemachine.open_session` (not this task's file) doesn't set it, so
    tests exercising the O6 gate force it here rather than editing that
    module."""
    ws.lead_confirmed = False
    db_session.commit()


# -- P3-10: finish happy path --------------------------------------------------


def test_finish_happy_path_closes_session_advances_unit_and_enqueues_writeback(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator_with_employee(db_session, employee_code="42")
    work_order, plan_ops, units = _seed_work_order(db_session, op_work_centers=("CNC1", "ASSY1"))
    unit = units[0]
    box = _bind_box(db_session, unit)
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = _scan(client, box.qr_payload, token)
    assert resp.json()["code"] == "accepted"

    ws = _open_session_for(db_session, unit)
    _mark_step_done(db_session, unit, plan_ops[0])

    resp = client.post(
        f"/operations/{plan_ops[0].id}/finish", json={"unit_id": str(unit.id)},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == "finished"
    assert body["unit_status"] == "in_transit"
    assert len(body["outbox_ids"]) == 1  # single nested time_ticket write per session (CR-018)

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.status == "in_transit"
    assert unit.current_plan_op_id == plan_ops[1].id

    closed_ws = db_session.get(WorkSession, ws.id)
    assert closed_ws.ended_at is not None
    assert closed_ws.close_reason == "finished"

    rows = db_session.execute(select(JB2Outbox)).scalars().all()
    assert len(rows) == 1
    tt = rows[0]
    assert tt.kind == "time_ticket"

    expected_key = writer.make_key(
        "wo", work_order.id, "unit", unit.unit_no, "op", plan_ops[0].seq, "session", ws.id,
    )
    assert tt.idempotency_key == expected_key
    payload = tt.payload
    assert payload["employeeCode"] == 42
    assert payload["allowClosedJobs"] is True
    assert "operationNumber" not in payload
    assert "workCenter" not in payload
    detail = payload["timeTicketDetails"][0]
    assert detail["jobNumber"] == "10008-01"
    assert detail["stepNumber"] == 1
    assert detail["piecesFinished"] == 1
    assert detail["piecesScrapped"] == 0
    assert detail["timeStart"] is not None
    assert len(detail["timeStart"]) <= 5  # HH:MM, not ISO (findings §2 item 2)
    assert "cycleTime" not in detail  # JB2 derives it, we never send it


def test_finish_blocked_when_steps_open(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator_with_employee(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]
    box = _bind_box(db_session, unit)
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    _scan(client, box.qr_payload, token)

    resp = client.post(
        f"/operations/{plan_ops[0].id}/finish", json={"unit_id": str(unit.id)},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 409

    db_session.expire_all()
    assert db_session.get(Unit, unit.id).status == "at_station"


def test_double_finish_is_idempotent_noop(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator_with_employee(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]
    box = _bind_box(db_session, unit)
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    _scan(client, box.qr_payload, token)
    _mark_step_done(db_session, unit, plan_ops[0])

    r1 = client.post(
        f"/operations/{plan_ops[0].id}/finish", json={"unit_id": str(unit.id)},
        headers={"X-Station-Token": token},
    )
    assert r1.json()["code"] == "finished"

    rows_after_first = db_session.execute(select(JB2Outbox)).scalars().all()

    r2 = client.post(
        f"/operations/{plan_ops[0].id}/finish", json={"unit_id": str(unit.id)},
        headers={"X-Station-Token": token},
    )
    assert r2.status_code == 200
    assert r2.json()["code"] == "already_finished"

    rows_after_second = db_session.execute(select(JB2Outbox)).scalars().all()
    assert len(rows_after_second) == len(rows_after_first)  # no duplicate write-back


def test_finish_last_op_serial_gate_both_ways(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator_with_employee(db_session)
    _, plan_ops, units = _seed_work_order(db_session, op_work_centers=("CNC1",))
    unit = units[0]
    box = _bind_box(db_session, unit)
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    _scan(client, box.qr_payload, token)
    _mark_step_done(db_session, unit, plan_ops[0])

    # no serial yet -- blocked (REQUIRE_SERIAL_BEFORE_DONE defaults true)
    resp = client.post(
        f"/operations/{plan_ops[0].id}/finish", json={"unit_id": str(unit.id)},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 422

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.status == "at_station"
    unit.serial_number = "SN-0001"
    db_session.commit()

    resp = client.post(
        f"/operations/{plan_ops[0].id}/finish", json={"unit_id": str(unit.id)},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "finished"
    assert body["unit_status"] == "done"

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.status == "done"
    assert unit.completed_at is not None


def test_operator_without_jb2_employee_records_mapping_exception_not_outbox(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator(db_session)  # no jb2_employee_id link
    _, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]
    box = _bind_box(db_session, unit)
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    _scan(client, box.qr_payload, token)
    _mark_step_done(db_session, unit, plan_ops[0])

    resp = client.post(
        f"/operations/{plan_ops[0].id}/finish", json={"unit_id": str(unit.id)},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "finished"
    assert body["outbox_ids"] == []  # skipped, not enqueued

    assert db_session.execute(select(JB2Outbox)).scalars().all() == []
    exc = db_session.execute(
        select(MappingException).where(MappingException.kind == "jb2_employee")
    ).scalars().one()
    assert str(operator.id) == exc.value


# -- P3-08: scrap ---------------------------------------------------------------


def test_scrap_flow_creates_scrap_event_releases_box_replacement_and_enqueues_pieces_scrapped(
    db_session,
):
    station, _ = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator_with_employee(db_session, employee_code="99")
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    work_order, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]
    box = _bind_box(db_session, unit)

    failure_code = FailureCode(
        id=uuid.uuid4(), product_id=None, code="CRACK", label="Cracked frame",
        jb2_reason_number=21, active=True,
    )
    db_session.add(failure_code)
    db_session.add(
        JB2OrderMaterial(
            id=uuid.uuid4(), jb2_id=f"jm-{uuid.uuid4()}",
            jb2_line_item_id=work_order.jb2_line_item_id, routing_seq=1,
            part_number="FRAME-1", qty_planned=1, unit_cost=150.0, payload={}, **_mirror_common(),
        )
    )
    db_session.commit()

    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass",
    )
    db_session.commit()

    failure = failures.record_failure(
        db_session, unit, plan_ops[0], None, failure_code.id, "frame cracked at CNC",
        detected_by=operator, disposition="scrap", authorized_by=lead, remake=True,
    )
    db_session.commit()

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.status == "scrapped"

    box = db_session.get(BuildBox, box.id)
    assert box.current_unit_id is None
    assignment = db_session.execute(
        select(BoxAssignment).where(BoxAssignment.box_id == box.id)
    ).scalars().one()
    assert assignment.released_at is not None

    scrap_event = db_session.execute(
        select(ScrapEvent).where(ScrapEvent.failure_id == failure.id)
    ).scalars().one()
    assert scrap_event.material_value_est == pytest.approx(150.0)
    assert scrap_event.replacement_unit_id is not None

    replacement = db_session.get(Unit, scrap_event.replacement_unit_id)
    assert replacement.remake_of_unit_id == unit.id
    assert replacement.unit_no == 2

    closed_ws = db_session.get(WorkSession, ws.id)
    assert closed_ws.ended_at is not None
    assert closed_ws.close_reason == "finished"

    tt = db_session.execute(
        select(JB2Outbox).where(JB2Outbox.kind == "time_ticket")
    ).scalars().one()
    detail = tt.payload["timeTicketDetails"][0]
    assert detail["piecesScrapped"] == 1
    assert detail["piecesFinished"] == 0
    assert detail["reasonNumber"] == 21


def test_scrap_requires_lead_authorizer(db_session):
    station, _ = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]
    failure_code = FailureCode(id=uuid.uuid4(), product_id=None, code="X", label="X", active=True)
    db_session.add(failure_code)
    db_session.commit()

    with pytest.raises(failures.LeadRequiredError):
        failures.record_failure(
            db_session, unit, plan_ops[0], None, failure_code.id, "oops",
            detected_by=operator, disposition="scrap", authorized_by=operator,  # not a lead
        )


# -- P3-08: rework_to_op --------------------------------------------------------


def test_rework_to_op_resets_range_and_flips_first_pass_permanently(db_session):
    station, _ = _make_station(db_session, work_center_code="ASSY3")
    operator = _make_operator(db_session)
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    _, plan_ops, units = _seed_work_order(db_session, op_work_centers=("CNC1", "ASSY1", "ASSY3"))
    unit = units[0]
    op1, op2, op3 = plan_ops

    for op in (op1, op2, op3):
        db_session.add(
            StepExecution(
                plan_operation_id=op.id, unit_id=unit.id, step_seq=1, status="done",
                superseded=False,
            )
        )
    unit.current_plan_op_id = op3.id
    unit.status = "at_station"
    db_session.commit()

    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=op3, kind="first_pass",
    )
    db_session.commit()

    failure_code = FailureCode(
        id=uuid.uuid4(), product_id=None, code="MISALIGN", label="Misaligned", active=True,
    )
    db_session.add(failure_code)
    db_session.commit()

    failures.record_failure(
        db_session, unit, op3, None, failure_code.id, "frame misaligned at final assy",
        detected_by=operator, disposition="rework_to_op", rework_to_op=op2, authorized_by=lead,
    )
    db_session.commit()

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.first_pass is False
    assert unit.rework_count == 1
    assert unit.current_plan_op_id == op2.id
    assert unit.status == "in_transit"

    def _step_exec(op):
        return db_session.execute(
            select(StepExecution).where(
                StepExecution.plan_operation_id == op.id, StepExecution.unit_id == unit.id
            )
        ).scalars().first()

    assert _step_exec(op1).superseded is False  # before K -- untouched
    assert _step_exec(op2).superseded is True
    assert _step_exec(op3).superseded is True

    closed_ws = db_session.get(WorkSession, ws.id)
    assert closed_ws.ended_at is not None
    assert closed_ws.close_reason == "clocked_out"

    # v1 simplification (see task report): no time-ticket-detail enqueued at
    # reopen time -- only finish/scrap/lead-confirm produce outbox writes.
    assert db_session.execute(select(JB2Outbox)).scalars().all() == []


# -- P3-09: idle sweep + O6 lead confirm ---------------------------------------


def test_idle_sweep_pauses_then_auto_closes(db_session, engine):
    station, _ = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]

    now = datetime.now(timezone.utc)
    stale_start = now - timedelta(minutes=real_config.station_session_idle_min + 5)
    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass", now=stale_start,
    )
    db_session.commit()

    session_factory = sessionmaker(bind=engine)
    sessions_domain.run_idle_sweep(session_factory, now=now)

    db_session.expire_all()
    ws = db_session.get(WorkSession, ws.id)
    assert ws.ended_at is None  # not closed yet, just paused
    pause = db_session.execute(
        select(SessionPause).where(
            SessionPause.work_session_id == ws.id, SessionPause.ended_at.is_(None)
        )
    ).scalars().one()
    assert pause.reason_code == "other"

    later = now + timedelta(minutes=real_config.session_auto_close_min + 5)
    sessions_domain.run_idle_sweep(session_factory, now=later)

    db_session.expire_all()
    ws = db_session.get(WorkSession, ws.id)
    assert ws.ended_at is not None
    assert ws.close_reason == "auto_closed"
    # lead_confirmed's own default correctness is covered by
    # test_auto_closed_session_withholds_outbox_until_lead_confirm (which
    # forces it past the sqlite server_default quirk, see
    # _force_not_confirmed) -- this test's job is just the pause-then-close
    # timing, not that field.


def test_auto_closed_session_withholds_outbox_until_lead_confirm(db_session):
    station, _ = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator_with_employee(db_session, employee_code="77")
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    _, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]

    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass",
    )
    _force_not_confirmed(db_session, ws)

    now = datetime.now(timezone.utc)
    old_pause_start = now - timedelta(minutes=real_config.session_auto_close_min + 10)
    db_session.add(
        SessionPause(work_session_id=ws.id, reason_code="other", started_at=old_pause_start)
    )
    db_session.commit()

    closed = statemachine.auto_close_idle(
        db_session, now, idle_paused_minutes=real_config.session_auto_close_min
    )
    db_session.commit()
    assert len(closed) == 1
    assert db_session.execute(select(JB2Outbox)).scalars().all() == []  # withheld

    db_session.expire_all()
    ws = db_session.get(WorkSession, ws.id)
    assert ws.close_reason == "auto_closed"

    sessions_domain.lead_confirm_session(db_session, ws, lead)
    db_session.commit()

    db_session.expire_all()
    ws = db_session.get(WorkSession, ws.id)
    assert ws.lead_confirmed is True
    tt = db_session.execute(
        select(JB2Outbox).where(JB2Outbox.kind == "time_ticket")
    ).scalars().one()
    assert tt.payload["timeTicketDetails"][0]["piecesFinished"] == 0


def test_lead_confirm_requires_lead_role(db_session):
    station, _ = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator_with_employee(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]
    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass",
    )
    statemachine.close_session(db_session, ws, reason="auto_closed")
    db_session.commit()

    with pytest.raises(sessions_domain.LeadRequiredError):
        sessions_domain.lead_confirm_session(db_session, ws, operator)  # operator, not lead


# -- P3-10 x P1-09: drain against fake-JB2, CR-010 write-set check -------------


def test_drain_against_fake_jb2_matches_expected_payload_and_zero_routing_patches(db_session):
    station, _ = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator_with_employee(db_session, employee_code="55")
    work_order, plan_ops, units = _seed_work_order(db_session, op_work_centers=("CNC1", "ASSY1"))
    unit = units[0]

    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass",
    )
    statemachine.close_session(db_session, ws, reason="finished")
    db_session.commit()

    from app.outbox import payloads

    payloads.enqueue_finish_writeback(db_session, ws, unit, plan_ops[0], pieces_finished=1)
    db_session.commit()

    state = seed_state()
    app = create_fake_jb2(state)
    transport = TestClient(app)._transport
    jb2_client = Jb2Client(
        "https://api-jb2.example.com", "https://auth-jb2.example.com",
        "test-client-id", "test-client-secret", transport=transport, sleeper=lambda s: None,
    )
    try:
        outcomes = drainer.drain_once(db_session, jb2_client)
    finally:
        jb2_client.close()

    assert all(outcome == "confirmed" for _id, outcome in outcomes)

    writes = state["received_writes"]
    assert {w["path"] for w in writes} == {"/time-tickets"}  # single nested write (CR-018)
    assert not any(w["path"].startswith("/order-routings") for w in writes)  # CR-010

    tt_write = next(w for w in writes if w["path"] == "/time-tickets")
    assert tt_write["body"]["employeeCode"] == 55
    detail = tt_write["body"]["timeTicketDetails"][0]
    assert detail["jobNumber"] == "10008-01"
    assert detail["stepNumber"] == 1
    assert detail["piecesFinished"] == 1
