"""Integration tests for P4-06 backfill -- DD §17.2.

Zero network: sqlite in-memory DB, same domain-level seeding pattern as
tests/integration/test_finish_and_writeback.py (WorkOrder/PlanOperation/Unit
inserted directly, bypassing app.domain.workorders). Exercises the real
`/admin/backfill/{unit_id}` HTTP route (app/api/backfill.py) rather than
calling its internals directly, so the test proves the same thing a lead
filling out the form would get.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.backfill import router as backfill_router
from app.auth import service
from app.db import get_session
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    Event,
    Measurement,
    StepExecution,
    SubstepExecution,
    WorkSession,
)
from app.domain.models_jb2 import Base, JB2Employee, JB2OrderLineItem, JB2OrderRouting, JB2Outbox
from app.domain.models_library import Product

# Registers the full model graph (work_orders etc.) on the shared Base.metadata.
from app.main import app as _main_app  # noqa: F401

MEASUREMENT_SPEC = {"nominal": 1.0, "tol_plus": 0.01, "tol_minus": 0.01, "unit": "in"}


def _sub(seq, type_, title, *, spec=None, signoff_role=None):
    return {
        "seq": seq, "type": type_, "title": title, "body_html": None, "required": True,
        "measurement_spec": spec, "media": [], "signoff_role": signoff_role,
    }


FROZEN_STEP = {
    "seq": 1, "title": "Assemble", "body_html": None, "est_minutes": 5,
    "substeps": [
        _sub(1, "action", "Clean part"),
        _sub(2, "measurement", "Bore diameter", spec=MEASUREMENT_SPEC),
    ],
}


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


@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(backfill_router)

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


def _mirror_common() -> dict:
    return {"content_hash": "h", "jb2_last_modified": None, "synced_at": datetime.now(timezone.utc)}


def _seed_unit(db_session: Session, *, op_work_centers=("CNC1", "ASSY1")):
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=1,
        payload={"jobNumber": "10008-01", "orderNumber": "10008"}, **_mirror_common(),
    )
    db_session.add(line_item)
    db_session.flush()

    product = Product(id=uuid.uuid4(), name="Apollo", variant_schema={}, active=True)
    db_session.add(product)
    db_session.flush()

    work_order = WorkOrder(
        id=uuid.uuid4(), jb2_line_item_id=line_item.id, product_id=product.id,
        qty=1, status="in_progress",
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
            id=uuid.uuid4(), work_order_id=work_order.id, seq=seq, jb2_routing_id=routing.id,
            operation_code=routing.operation_code, title=f"Op {seq} ({wc})",
            frozen_content={"steps": [FROZEN_STEP]}, status="pending",
        )
        db_session.add(plan_op)
        plan_ops.append(plan_op)
    db_session.flush()

    unit = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=1, status="queued",
        first_pass=True, rework_count=0,
    )
    db_session.add(unit)
    db_session.commit()
    return work_order, plan_ops, unit


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


def _make_lead(db_session, name="Lead"):
    lead = service.create_operator(db_session, display_name=name, roles=["operator", "lead"])
    db_session.commit()
    return lead


def _submit(client, unit_id, *, performed_by, lead, paper_at, finish=False, extra=None):
    form = {
        "performed_by_operator_id": str(performed_by.id),
        "lead_operator_id": str(lead.id),
        "paper_at": paper_at.isoformat(),
        "sub_1_1_action": "done",
        "sub_1_1_notes": "cleaned per traveler",
        "sub_1_2_action": "measurement",
        "sub_1_2_value": "1.000",
    }
    if finish:
        form["finish"] = "on"
    if extra:
        form.update(extra)
    return client.post(f"/admin/backfill/{unit_id}", data=form, follow_redirects=False)


# -- happy path: same substep/finish effects + outbox write as live ------------


def test_backfill_records_substeps_measurement_and_finish_like_live_station(client, db_session):
    work_order, plan_ops, unit = _seed_unit(db_session)
    performed_by = _make_operator_with_employee(db_session, employee_code="42")
    lead = _make_lead(db_session)
    paper_at = datetime.now(timezone.utc) - timedelta(days=1)

    resp = _submit(
        client, unit.id, performed_by=performed_by, lead=lead, paper_at=paper_at, finish=True,
    )
    assert resp.status_code == 303, resp.text
    assert "ok=1" in resp.headers["location"]

    db_session.expire_all()

    step_exec = db_session.execute(
        select(StepExecution).where(
            StepExecution.plan_operation_id == plan_ops[0].id, StepExecution.unit_id == unit.id,
        )
    ).scalars().one()
    assert step_exec.status == "done"

    action_sub = db_session.execute(
        select(SubstepExecution).where(
            SubstepExecution.step_execution_id == step_exec.id, SubstepExecution.substep_seq == 1,
        )
    ).scalars().one()
    assert action_sub.status == "done"
    assert action_sub.notes.startswith("[BACKFILLED]")

    measurement_sub = db_session.execute(
        select(SubstepExecution).where(
            SubstepExecution.step_execution_id == step_exec.id, SubstepExecution.substep_seq == 2,
        )
    ).scalars().one()
    assert measurement_sub.status == "done"
    assert measurement_sub.pass_ is True

    measurement_row = db_session.execute(
        select(Measurement).where(Measurement.substep_execution_id == measurement_sub.id)
    ).scalars().one()
    assert float(measurement_row.value) == pytest.approx(1.0)

    # unit advanced to the second op -- identical effect to a live finish.
    unit = db_session.get(Unit, unit.id)
    assert unit.status == "in_transit"
    assert unit.current_plan_op_id == plan_ops[1].id

    ws = db_session.execute(
        select(WorkSession).where(WorkSession.unit_id == unit.id)
    ).scalars().one()
    assert ws.ended_at is not None
    assert ws.close_reason == "finished"
    assert ws.operator_id == performed_by.id

    # identical outbox write to a live finish: one nested time_ticket row
    # (CR-018 -- header+detail ride together, no separate header row).
    outbox_rows = db_session.execute(select(JB2Outbox)).scalars().all()
    assert len(outbox_rows) == 1
    tt = outbox_rows[0]
    assert tt.kind == "time_ticket"
    assert tt.payload["employeeCode"] == 42
    detail = tt.payload["timeTicketDetails"][0]
    assert detail["jobNumber"] == "10008-01"
    assert detail["piecesFinished"] == 1


def test_backfill_marker_present_in_event_timeline(client, db_session):
    """The backfilled substep/measurement/finish rows carry an extra
    `backfill.recorded` event on the same entity a live call would have
    touched -- that's what makes them distinguishable in any per-entity
    unit timeline without a schema change."""
    work_order, plan_ops, unit = _seed_unit(db_session)
    performed_by = _make_operator_with_employee(db_session)
    lead = _make_lead(db_session)
    paper_at = datetime.now(timezone.utc) - timedelta(hours=6)

    _submit(client, unit.id, performed_by=performed_by, lead=lead, paper_at=paper_at, finish=True)
    db_session.expire_all()

    backfill_events = db_session.execute(
        select(Event).where(Event.verb == "backfill.recorded")
    ).scalars().all()
    # one per substep touched (2) + one for the finish/session close.
    assert len(backfill_events) == 3
    for evt in backfill_events:
        assert evt.after["backfilled"] is True
        assert evt.after["lead_id"] == str(lead.id)
        assert evt.after["performed_by"] == str(performed_by.id)
        assert evt.after["paper_at"] == paper_at.isoformat()
        assert evt.actor_id == lead.id

    # a normal substep.done event exists too (the live-identical effect) --
    # the backfill event rides alongside it, doesn't replace it.
    normal_events = db_session.execute(
        select(Event).where(Event.verb.in_(("substep.done", "measurement.recorded")))
    ).scalars().all()
    assert len(normal_events) >= 2


def test_backfill_requires_a_lead(client, db_session):
    work_order, plan_ops, unit = _seed_unit(db_session)
    performed_by = _make_operator_with_employee(db_session)
    not_a_lead = service.create_operator(db_session, display_name="Bob", roles=["operator"])
    db_session.commit()
    paper_at = datetime.now(timezone.utc)

    resp = _submit(
        client, unit.id, performed_by=performed_by, lead=not_a_lead, paper_at=paper_at,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert "lead" in resp.headers["location"].lower()

    db_session.expire_all()
    # nothing committed -- no WorkSession/StepExecution created.
    assert db_session.execute(select(WorkSession)).scalars().all() == []


def test_backfill_missing_lead_id_rejected(client, db_session):
    work_order, plan_ops, unit = _seed_unit(db_session)
    performed_by = _make_operator_with_employee(db_session)
    paper_at = datetime.now(timezone.utc)

    form = {
        "performed_by_operator_id": str(performed_by.id),
        "lead_operator_id": "",
        "paper_at": paper_at.isoformat(),
        "sub_1_1_action": "done",
    }
    resp = client.post(f"/admin/backfill/{unit.id}", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


# -- included in a metrics count -------------------------------------------------


def test_backfilled_substeps_are_included_in_a_plain_metrics_count(client, db_session):
    """No `backfilled` column exists (by design, see app/api/backfill.py's
    module docstring) -- a backfilled substep is just a normal `done` row in
    `substep_executions`, so any metrics query counting done substeps for
    this operation includes it automatically. This test proxies that with
    the simplest possible 'metric': a count query."""
    work_order, plan_ops, unit = _seed_unit(db_session)
    performed_by = _make_operator_with_employee(db_session)
    lead = _make_lead(db_session)
    paper_at = datetime.now(timezone.utc) - timedelta(hours=2)

    before_count = db_session.execute(
        select(SubstepExecution).where(SubstepExecution.status == "done")
    ).scalars().all()
    assert len(before_count) == 0

    _submit(client, unit.id, performed_by=performed_by, lead=lead, paper_at=paper_at, finish=False)
    db_session.expire_all()

    after_count = db_session.execute(
        select(SubstepExecution).where(SubstepExecution.status == "done")
    ).scalars().all()
    assert len(after_count) == 2  # the action + in-tolerance measurement

    # partial (non-finish) backfill leaves the session closed as clocked_out,
    # not dangling open, and does NOT advance/enqueue anything.
    ws = db_session.execute(
        select(WorkSession).where(WorkSession.unit_id == unit.id)
    ).scalars().one()
    assert ws.ended_at is not None
    assert ws.close_reason == "clocked_out"
    unit = db_session.get(Unit, unit.id)
    assert unit.status == "queued"  # unchanged -- finish wasn't requested
    assert db_session.execute(select(JB2Outbox)).scalars().all() == []


def test_backfill_skip_requires_no_extra_wiring_and_marks_event(client, db_session):
    work_order, plan_ops, unit = _seed_unit(db_session)
    performed_by = _make_operator_with_employee(db_session)
    lead = _make_lead(db_session)
    paper_at = datetime.now(timezone.utc) - timedelta(hours=3)

    resp = _submit(
        client, unit.id, performed_by=performed_by, lead=lead, paper_at=paper_at,
        extra={"sub_1_1_action": "skip", "sub_1_1_skip_reason": "paper illegible"},
    )
    assert resp.status_code == 303
    assert "error=" not in resp.headers["location"]

    db_session.expire_all()
    step_exec = db_session.execute(select(StepExecution)).scalars().one()
    action_sub = db_session.execute(
        select(SubstepExecution).where(
            SubstepExecution.step_execution_id == step_exec.id, SubstepExecution.substep_seq == 1,
        )
    ).scalars().one()
    assert action_sub.status == "skipped"
    assert action_sub.skip_reason == "paper illegible"
    assert action_sub.skip_authorized_by == lead.id
