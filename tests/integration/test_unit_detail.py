"""Integration tests for P4-03 (unit drill-down, DD §13.1/§9.1-9.4).

Same zero-network sqlite pattern as tests/integration/test_fail_flow.py and
tests/integration/test_finish_and_writeback.py: walks a unit through two
operations (a scan + action-substep completion at op1, then a scan +
out-of-tolerance measurement + rework_in_place disposition at op2) using
domain functions directly (statemachine/substeps/boxes), then asserts the
GET /units/{id} HTML page and GET /api/v1/units/{id}/timeline JSON both
carry every fact from that walk, in chronological order.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.unit_detail import router as unit_detail_router
from app.auth import service
from app.db import get_session
from app.domain import boxes, statemachine, substeps
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import Event, WorkSession
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting
from app.domain.models_library import FailureCode, Product

# Registers the full model graph on Base.metadata (work_orders etc.) -- same
# convention as the other Phase-3/4 integration tests.
from app.main import app as _main_app  # noqa: F401

MEASUREMENT_SPEC = {
    "nominal": 1.0, "tol_plus": 0.005, "tol_minus": 0.005, "unit": "in", "decimals": 3,
}


def _mirror_common() -> dict:
    return {"content_hash": "h", "jb2_last_modified": None, "synced_at": datetime.now(timezone.utc)}


def _plain_content() -> dict:
    return {
        "instruction_set_id": str(uuid.uuid4()), "version": 3,
        "steps": [{
            "seq": 1, "title": "Rough machine", "body_html": None, "est_minutes": 5,
            "substeps": [{
                "seq": 1, "type": "action", "title": "Deburr edges", "body_html": None,
                "required": True, "measurement_spec": None, "media": [], "signoff_role": None,
            }],
        }],
    }


def _measure_content() -> dict:
    return {
        "instruction_set_id": str(uuid.uuid4()), "version": 1,
        "steps": [{
            "seq": 1, "title": "Final assembly", "body_html": None, "est_minutes": 8,
            "substeps": [{
                "seq": 1, "type": "measurement", "title": "Bore diameter", "body_html": None,
                "required": True, "measurement_spec": MEASUREMENT_SPEC, "media": [],
                "signoff_role": None,
            }],
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
def client(engine):
    app = FastAPI()
    app.include_router(unit_detail_router)

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


def _seed(db_session: Session):
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=1,
        payload={"jobNumber": "10021-01", "orderNumber": "10021"}, **_mirror_common(),
    )
    db_session.add(line_item)
    db_session.flush()

    product = Product(id=uuid.uuid4(), name="Apollo", variant_schema={}, active=True)
    db_session.add(product)
    db_session.flush()

    work_order = WorkOrder(
        id=uuid.uuid4(), jb2_line_item_id=line_item.id, product_id=product.id,
        qty=1, status="ready", plan_version=2,
    )
    db_session.add(work_order)
    db_session.flush()

    plan_ops = []
    for seq, (wc, content) in enumerate(
        [("CNC1", _plain_content()), ("ASSY1", _measure_content())], start=1
    ):
        routing = JB2OrderRouting(
            id=uuid.uuid4(), jb2_id=f"routing-{work_order.id}-{seq}",
            jb2_line_item_id=line_item.id, seq=seq, operation_code=f"OP{seq * 10}",
            description=f"Op {seq}", work_center_code=wc, payload={}, **_mirror_common(),
        )
        db_session.add(routing)
        db_session.flush()
        plan_op = PlanOperation(
            id=uuid.uuid4(), work_order_id=work_order.id, seq=seq, jb2_routing_id=routing.id,
            operation_code=f"OP{seq * 10}", title=f"Op {seq} ({wc})", frozen_content=content,
            status="pending", est_minutes=5,
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

    station1, _ = service.create_station(db_session, name="CNC bench", work_center_code="CNC1")
    station2, _ = service.create_station(db_session, name="Assy bench", work_center_code="ASSY1")
    operator = service.create_operator(db_session, display_name="Alice", roles=["operator"])
    failure_code = FailureCode(
        id=uuid.uuid4(), product_id=None, code="COSMETIC", label="Cosmetic scratch", active=True,
    )
    db_session.add(failure_code)
    db_session.commit()

    return work_order, plan_ops, unit, station1, station2, operator, failure_code


def _walk_unit(
    db_session: Session, work_order, plan_ops, unit, station1, station2, operator, failure_code
):
    op1, op2 = plan_ops

    box = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")
    boxes.assign_box(db_session, box, unit, operator, serial="SN-1001")
    db_session.commit()

    # -- op1: scan in, complete the one action substep, finish (domain-fn only)
    result1 = statemachine.resolve_scan(db_session, box.qr_payload, station1, operator)
    assert result1.code == "accepted"
    db_session.commit()

    substeps.complete_substep(
        db_session, station=station1, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=1,
    )
    db_session.commit()

    ws1 = db_session.execute(
        select(WorkSession).where(WorkSession.unit_id == unit.id, WorkSession.ended_at.is_(None))
    ).scalars().one()
    statemachine.close_session(db_session, ws1, reason="finished")
    # Bypasses app/api/operations.py's finish endpoint (a concurrent agent's
    # file, out of scope here) -- advance the position pointer/status by hand,
    # same shortcut tests/integration/test_fail_flow.py uses.
    unit.current_plan_op_id = op2.id
    unit.status = "in_transit"
    db_session.commit()

    # -- op2: scan in, out-of-tolerance measurement, rework_in_place disposition
    result2 = statemachine.resolve_scan(db_session, box.qr_payload, station2, operator)
    assert result2.code == "accepted"
    db_session.commit()

    substeps.record_measurement(
        db_session, station=station2, operator=operator, unit_id=unit.id,
        step_seq=1, substep_seq=1, value="1.500",
    )
    db_session.commit()

    substeps.apply_disposition(
        db_session, station=station2, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=1,
        disposition="rework_in_place", failure_code_id=failure_code.id,
        notes="cosmetic scratch, buffed out",
    )
    db_session.commit()

    ws2 = db_session.execute(
        select(WorkSession).where(WorkSession.unit_id == unit.id, WorkSession.ended_at.is_(None))
    ).scalars().one()
    statemachine.close_session(db_session, ws2, reason="finished")
    db_session.commit()


def test_timeline_page_and_json_contain_every_fact_chronologically(db_session, client):
    work_order, plan_ops, unit, station1, station2, operator, failure_code = _seed(db_session)
    _walk_unit(db_session, work_order, plan_ops, unit, station1, station2, operator, failure_code)

    # -- HTML page --------------------------------------------------------------
    page = client.get(f"/units/{unit.id}")
    assert page.status_code == 200, page.text
    html = page.text

    assert "Apollo" in html
    assert "SN-1001" in html  # §9.1 serial
    assert "10021-01" in html  # §9.1 JB2 job number
    assert "rework" in html.lower()  # rework banner (rework_count=1)
    assert "Deburr edges" in html  # §9.3 substep title
    assert "Bore diameter" in html  # §9.4 measurement name
    assert "1.5" in html  # measured value
    assert "OUT OF TOLERANCE" in html
    assert "Cosmetic scratch" in html  # failure code label
    assert "cosmetic scratch, buffed out" in html  # narrative
    assert "Alice" in html  # operator identity (§9.7)
    assert "CNC bench" in html and "Assy bench" in html  # station identity (§9.2)
    assert "accepted" in html  # scan result (§9.2)

    # -- JSON -------------------------------------------------------------------
    resp = client.get(f"/api/v1/units/{unit.id}/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["unit"]["serial_number"] == "SN-1001"
    assert body["unit"]["first_pass"] is False
    assert body["unit"]["rework_count"] == 1
    assert body["product"]["name"] == "Apollo"
    assert body["jb2"]["job_number"] == "10021-01"
    assert len(body["plan_operations"]) == 2
    assert body["plan_operations"][0]["operation_code"] == "OP10"
    assert body["plan_operations"][1]["instruction_version"] == 1

    kinds = [e["kind"] for e in body["timeline"]]
    for expected in (
        "box_assigned", "scan", "session_opened", "substep_done", "session_closed",
        "measurement", "substep_failed", "failure", "event_unit.reworked",
    ):
        assert expected in kinds, f"missing {expected!r} in timeline kinds: {kinds}"

    # chronological ordering, checked only across entries whose timestamps
    # are all app-level `datetime.now()` writes (session/substep/measurement/
    # failure) -- Scan.scanned_at and Event.at are DB `server_default=now()`
    # writes, which round-trip whole-seconds-only on sqlite (no
    # microseconds), so a same-second scan/event can legitimately land
    # earlier or later than a microsecond-precise sibling; that's a sqlite
    # storage-precision artifact (verify_migrations_on_pg confirms Postgres
    # doesn't lose precision), not a timeline-building bug, so it isn't
    # asserted here.
    timeline = body["timeline"]

    def _first_idx(kind: str) -> int:
        return next(i for i, e in enumerate(timeline) if e["kind"] == kind)

    assert _first_idx("session_opened") < _first_idx("substep_done")
    assert _first_idx("substep_done") < _first_idx("session_closed")
    assert _first_idx("measurement") < _first_idx("failure")

    measurement_entry = next(e for e in body["timeline"] if e["kind"] == "measurement")
    assert measurement_entry["value"] == 1.5
    assert measurement_entry["in_tolerance"] is False
    assert measurement_entry["operator"] == "Alice"

    failure_entry = next(e for e in body["timeline"] if e["kind"] == "failure")
    assert failure_entry["disposition"] == "rework_in_place"
    assert failure_entry["failure_label"] == "Cosmetic scratch"


def test_unit_not_found_is_404(client):
    resp = client.get(f"/units/{uuid.uuid4()}")
    assert resp.status_code == 404
    resp = client.get(f"/api/v1/units/{uuid.uuid4()}/timeline")
    assert resp.status_code == 404
