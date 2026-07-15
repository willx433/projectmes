"""Integration tests for P3-R2 (ESC-004 remediation): wiring the station
fail/scrap/rework disposition UI to app.domain.failures.record_failure.

Same zero-network sqlite pattern as tests/integration/test_station_flow.py
(station/substeps routers) and tests/integration/test_finish_and_writeback.py
(multi-op work order seeding) -- reuses both conventions rather than
inventing a third.

Covers:
  - out-of-tolerance measurement -> scrap disposition (lead badge) ->
    scrap_event + unit scrapped + failures row + box released.
  - out-of-tolerance measurement -> rework_to_op K (lead badge) -> ops
    K..N reset (superseded), first_pass false, unit in_transit to op K.
  - whole-operation Fail screen (GET /station/fail/{unit_id}) renders with
    the failure-code picker populated (product-scoped + global codes).
  - non-lead blocked on scrap/rework_to_op at the whole-op Fail screen.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.auth import router as auth_router
from app.api.scan import router as scan_router
from app.api.station import router as station_router
from app.api.substeps import router as substeps_router
from app.auth import service
from app.config import config as real_config
from app.db import get_session
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    BoxAssignment,
    BuildBox,
    Event,
    Failure,
    ScrapEvent,
    StepExecution,
    WorkSession,
)
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting
from app.domain.models_library import FailureCode, Product

# Registers the full model graph on Base.metadata (work_orders etc.).
from app.main import app as _main_app  # noqa: F401

TEST_SECRET = "unit-test-secret-key-not-for-prod"

MEASUREMENT_SPEC = {
    "nominal": 1.0, "tol_plus": 0.005, "tol_minus": 0.005, "unit": "in", "decimals": 3,
}


def _sub(seq, type_, title, *, spec=None):
    return {
        "seq": seq, "type": type_, "title": title, "body_html": None, "required": True,
        "measurement_spec": spec, "media": [], "signoff_role": None,
    }


def _measure_content():
    return {
        "instruction_set_id": str(uuid.uuid4()), "version": 1,
        "steps": [{
            "seq": 1, "title": "Measure", "body_html": None, "est_minutes": 5,
            "substeps": [_sub(1, "measurement", "Bore diameter", spec=MEASUREMENT_SPEC)],
        }],
    }


def _plain_content():
    return {
        "instruction_set_id": str(uuid.uuid4()), "version": 1,
        "steps": [{
            "seq": 1, "title": "Step 1", "body_html": None, "est_minutes": 5, "substeps": [],
        }],
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
def app_and_config(tmp_path):
    test_config = dataclasses.replace(
        real_config, mes_secret_key=TEST_SECRET, artifact_dir=str(tmp_path)
    )
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(scan_router)
    app.include_router(station_router)
    app.include_router(substeps_router)
    return app, test_config


@pytest.fixture
def client(engine, app_and_config, monkeypatch):
    app, test_config = app_and_config
    monkeypatch.setattr("app.api.auth.config", test_config)
    monkeypatch.setattr("app.api.scan.config", test_config)
    monkeypatch.setattr("app.api.station.config", test_config)
    monkeypatch.setattr("app.auth.deps.config", test_config)
    monkeypatch.setattr("app.domain.substeps.config", test_config)

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app, follow_redirects=False) as c:
        yield c


# -- seeding helpers -------------------------------------------------------------


def _mirror_common() -> dict:
    return {"payload": {}, "content_hash": "h", "jb2_last_modified": None,
            "synced_at": datetime.now(timezone.utc)}


def _seed_work_order(
    db_session: Session, *, op_work_centers: tuple[str, ...], contents: list[dict] | None = None,
) -> tuple[WorkOrder, list[PlanOperation], Unit, Product]:
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=1, **_mirror_common(),
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

    contents = contents or [_plain_content()] * len(op_work_centers)
    plan_ops = []
    for seq, (wc, content) in enumerate(zip(op_work_centers, contents), start=1):
        routing = JB2OrderRouting(
            id=uuid.uuid4(), jb2_id=f"routing-{work_order.id}-{seq}",
            jb2_line_item_id=line_item.id, seq=seq, operation_code=f"OP{seq * 10}",
            description=f"Op {seq}", work_center_code=wc, **_mirror_common(),
        )
        db_session.add(routing)
        db_session.flush()
        plan_op = PlanOperation(
            id=uuid.uuid4(), work_order_id=work_order.id, seq=seq, jb2_routing_id=routing.id,
            operation_code=f"OP{seq * 10}", title=f"Op {seq} ({wc})", frozen_content=content,
            status="pending", est_minutes=5, blocked=False,
        )
        db_session.add(plan_op)
        plan_ops.append(plan_op)
    db_session.flush()

    unit = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=1, status="queued",
        first_pass=True, rework_count=0,
    )
    db_session.add(unit)
    db_session.flush()

    return work_order, plan_ops, unit, product


def _bind_box(db_session: Session, unit: Unit) -> BuildBox:
    box = BuildBox(id=uuid.uuid4(), qr_payload=f"BOX:{uuid.uuid4()}", current_unit_id=unit.id)
    db_session.add(box)
    db_session.flush()
    db_session.add(BoxAssignment(id=uuid.uuid4(), box_id=box.id, unit_id=unit.id))
    db_session.flush()
    return box


def _make_station(db_session, *, work_center_code):
    station, token = service.create_station(
        db_session, name=f"Station-{work_center_code}", work_center_code=work_center_code
    )
    db_session.commit()
    return station, token


def _make_operator(db_session, *, name="Operator", roles=None):
    operator = service.create_operator(db_session, display_name=name, roles=roles or ["operator"])
    db_session.commit()
    return operator


def _badge_in(client, *, badge_qr, station_token):
    resp = client.post(
        "/scan", json={"payload": badge_qr}, headers={"X-Station-Token": station_token}
    )
    assert resp.status_code == 200, resp.text
    return resp


def _scan_box(client, *, box_payload, station_token):
    resp = client.post(
        "/scan", json={"payload": box_payload}, headers={"X-Station-Token": station_token}
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == "accepted", resp.json()
    return resp


HEADERS = lambda token: {"X-Station-Token": token}  # noqa: E731


def _measure_out_of_tolerance(client, *, unit_id, token):
    resp = client.post(
        f"/station/units/{unit_id}/substeps/1/1/measurement",
        data={"value": "1.500", "request_id": f"r-meas-{uuid.uuid4()}"}, headers=HEADERS(token),
    )
    assert resp.status_code == 303
    return resp


def _disposition(client, *, unit_id, token, disposition, failure_code_id, override_badge="",
                  rework_to_op_seq=None):
    data = {
        "disposition": disposition, "failure_code_id": str(failure_code_id),
        "request_id": f"r-disp-{uuid.uuid4()}",
    }
    if override_badge:
        data["override_badge"] = override_badge
    if rework_to_op_seq is not None:
        data["rework_to_op_seq"] = str(rework_to_op_seq)
    return client.post(
        f"/station/units/{unit_id}/substeps/1/1/disposition", data=data, headers=HEADERS(token),
    )


# -- out-of-tolerance -> scrap ----------------------------------------------------


def test_out_of_tolerance_scrap_by_lead_creates_scrap_event_and_releases_box(client, db_session):
    work_order, plan_ops, unit, product = _seed_work_order(
        db_session, op_work_centers=("ASSY1",), contents=[_measure_content()],
    )
    box = _bind_box(db_session, unit)
    failure_code = FailureCode(id=uuid.uuid4(), product_id=None, code="CRACK", label="Cracked")
    db_session.add(failure_code)
    db_session.commit()

    station, token = _make_station(db_session, work_center_code="ASSY1")
    operator = _make_operator(db_session, name="Op1")
    lead = _make_operator(db_session, name="Lead1", roles=["operator", "lead"])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    _scan_box(client, box_payload=box.qr_payload, station_token=token)

    _measure_out_of_tolerance(client, unit_id=unit.id, token=token)
    resp = _disposition(
        client, unit_id=unit.id, token=token, disposition="scrap",
        failure_code_id=failure_code.id, override_badge=lead.badge_qr,
    )
    assert resp.status_code == 303
    assert "error" not in resp.headers["location"]

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.status == "scrapped"

    failure = db_session.execute(
        select(Failure).where(Failure.unit_id == unit.id, Failure.disposition == "scrap")
    ).scalars().one()
    assert failure.failure_code_id == failure_code.id
    assert failure.authorized_by == lead.id

    scrap_event = db_session.execute(
        select(ScrapEvent).where(ScrapEvent.failure_id == failure.id)
    ).scalars().one()
    assert scrap_event.authorized_by == lead.id

    box = db_session.get(BuildBox, box.id)
    assert box.current_unit_id is None
    assignment = db_session.execute(
        select(BoxAssignment).where(BoxAssignment.box_id == box.id)
    ).scalars().one()
    assert assignment.released_at is not None


# -- out-of-tolerance -> rework_to_op ---------------------------------------------


def test_out_of_tolerance_rework_to_op_resets_range_and_moves_unit(client, db_session):
    work_order, plan_ops, unit, product = _seed_work_order(
        db_session, op_work_centers=("CNC1", "ASSY1"),
        contents=[_plain_content(), _measure_content()],
    )
    op1, op2 = plan_ops
    # simulate op1 already completed for this unit, so the reset range can be
    # observed superseding a row that predates the failure.
    db_session.add(
        StepExecution(
            plan_operation_id=op1.id, unit_id=unit.id, step_seq=1, status="done",
            superseded=False,
        )
    )
    unit.current_plan_op_id = op2.id
    unit.status = "at_station"
    db_session.commit()

    box = _bind_box(db_session, unit)
    failure_code = FailureCode(
        id=uuid.uuid4(), product_id=None, code="MISALIGN", label="Misaligned",
    )
    db_session.add(failure_code)
    db_session.commit()

    station, token = _make_station(db_session, work_center_code="ASSY1")
    operator = _make_operator(db_session, name="Op1")
    lead = _make_operator(db_session, name="Lead1", roles=["operator", "lead"])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    _scan_box(client, box_payload=box.qr_payload, station_token=token)

    _measure_out_of_tolerance(client, unit_id=unit.id, token=token)
    resp = _disposition(
        client, unit_id=unit.id, token=token, disposition="rework_to_op",
        failure_code_id=failure_code.id, override_badge=lead.badge_qr, rework_to_op_seq=op1.seq,
    )
    assert resp.status_code == 303
    assert "error" not in resp.headers["location"]

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.first_pass is False
    assert unit.rework_count == 1
    assert unit.current_plan_op_id == op1.id
    assert unit.status == "in_transit"

    op1_step = db_session.execute(
        select(StepExecution).where(
            StepExecution.plan_operation_id == op1.id, StepExecution.unit_id == unit.id
        )
    ).scalars().one()
    assert op1_step.superseded is True

    op2_step = db_session.execute(
        select(StepExecution).where(
            StepExecution.plan_operation_id == op2.id, StepExecution.unit_id == unit.id
        )
    ).scalars().one()
    assert op2_step.superseded is True

    failure = db_session.execute(
        select(Failure).where(Failure.unit_id == unit.id, Failure.disposition == "rework_to_op")
    ).scalars().one()
    assert failure.rework_to_op == op1.id

    closed_ws = db_session.execute(select(WorkSession)).scalars().one()
    assert closed_ws.ended_at is not None
    assert closed_ws.close_reason == "clocked_out"


def test_out_of_tolerance_non_lead_blocked_on_scrap_and_rework_to_op(client, db_session):
    work_order, plan_ops, unit, product = _seed_work_order(
        db_session, op_work_centers=("ASSY1",), contents=[_measure_content()],
    )
    box = _bind_box(db_session, unit)
    failure_code = FailureCode(id=uuid.uuid4(), product_id=None, code="CRACK", label="Cracked")
    db_session.add(failure_code)
    db_session.commit()

    station, token = _make_station(db_session, work_center_code="ASSY1")
    operator = _make_operator(db_session, name="Op1")
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    _scan_box(client, box_payload=box.qr_payload, station_token=token)
    _measure_out_of_tolerance(client, unit_id=unit.id, token=token)

    resp = _disposition(
        client, unit_id=unit.id, token=token, disposition="scrap",
        failure_code_id=failure_code.id, override_badge=operator.badge_qr,  # not a lead
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.status != "scrapped"
    assert db_session.execute(select(Failure)).scalars().first() is None


# -- whole-operation Fail screen ---------------------------------------------------


def test_fail_screen_renders_with_failure_code_picker_product_and_global(client, db_session):
    work_order, plan_ops, unit, product = _seed_work_order(
        db_session, op_work_centers=("ASSY1",),
    )
    product_code = FailureCode(
        id=uuid.uuid4(), product_id=product.id, code="P-DENT", label="Product-specific dent",
    )
    global_code = FailureCode(
        id=uuid.uuid4(), product_id=None, code="G-MISC", label="Global misc failure",
    )
    db_session.add_all([product_code, global_code])
    db_session.commit()

    station, token = _make_station(db_session, work_center_code="ASSY1")
    operator = _make_operator(db_session, name="Op1")
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = client.get(f"/station/fail/{unit.id}", headers=HEADERS(token))
    assert resp.status_code == 200
    body = resp.text
    assert "failure_code_id" in body
    assert "P-DENT" in body
    assert "G-MISC" in body
    assert "rework_to_op_seq" in body
    assert 'name="disposition"' in body


def test_fail_screen_blocks_non_lead_on_scrap_and_rework_to_op(client, db_session):
    work_order, plan_ops, unit, product = _seed_work_order(
        db_session, op_work_centers=("CNC1", "ASSY1"),
    )
    op1, op2 = plan_ops
    failure_code = FailureCode(id=uuid.uuid4(), product_id=None, code="X", label="X")
    db_session.add(failure_code)
    db_session.commit()

    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator(db_session, name="Op1")
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = client.post(
        f"/station/fail/{unit.id}",
        data={
            "disposition": "scrap", "failure_code_id": str(failure_code.id),
            "override_badge": operator.badge_qr,  # not a lead
        },
        headers=HEADERS(token),
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    resp = client.post(
        f"/station/fail/{unit.id}",
        data={
            "disposition": "rework_to_op", "failure_code_id": str(failure_code.id),
            "rework_to_op_seq": str(op1.seq), "override_badge": operator.badge_qr,
        },
        headers=HEADERS(token),
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.status != "scrapped"
    assert unit.first_pass is True
    assert db_session.execute(select(Failure)).scalars().first() is None


def test_fail_screen_rework_in_place_whole_op_flips_first_pass(client, db_session):
    work_order, plan_ops, unit, product = _seed_work_order(
        db_session, op_work_centers=("ASSY1",),
    )
    failure_code = FailureCode(id=uuid.uuid4(), product_id=None, code="X", label="X")
    db_session.add(failure_code)
    db_session.commit()

    station, token = _make_station(db_session, work_center_code="ASSY1")
    operator = _make_operator(db_session, name="Op1")
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = client.post(
        f"/station/fail/{unit.id}",
        data={
            "disposition": "rework_in_place", "failure_code_id": str(failure_code.id),
            "narrative": "cosmetic scratch, buffed out",
        },
        headers=HEADERS(token),
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/station/execute/{unit.id}"

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.first_pass is False
    assert unit.rework_count == 1
    failure = db_session.execute(select(Failure)).scalars().one()
    assert failure.disposition == "rework_in_place"
    assert failure.substep_execution_id is None
