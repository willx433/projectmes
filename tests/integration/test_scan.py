"""Integration tests for POST /scan + app/domain/statemachine.py (P3-04),
docs/state-machine.md S1-S10 (+O2 override). Zero-network sqlite, same
pattern as tests/integration/test_auth.py: a standalone FastAPI app (the
real scan_router + auth_router) against a fresh in-memory engine.

Seeding is domain-level (bypasses app.sync/app.domain.workorders): one
minimal jb2_order_line_item row satisfies the WorkOrder FK, then
WorkOrder/PlanOperation/Unit/BuildBox rows are inserted directly -- faster
and more direct for exercising each scan-table row in isolation, same
rationale as tests/integration/test_order_to_plan.py's domain-level tests.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.auth import router as auth_router
from app.api.scan import router as scan_router
from app.auth import service
from app.config import config as real_config
from app.db import get_session
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    BoxAssignment,
    BuildBox,
    Event,
    RequestDedup,
    Scan,
    Transit,
    WorkSession,
)
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting

# Registers the full model graph on Base.metadata (work_orders etc.).
from app.main import app as _main_app  # noqa: F401

TEST_SECRET = "unit-test-secret-key-not-for-prod"


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    # ponytail: same sqlite bigint-autoincrement workaround as test_auth.py.
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
    return {"payload": {}, "content_hash": "h", "jb2_last_modified": None,
            "synced_at": datetime.now(timezone.utc)}


def _seed_work_order(
    db_session: Session, *, qty: int = 1, op_work_centers: list[str] = ("CNC1", "ASSY1")
) -> tuple[WorkOrder, list[PlanOperation], list[Unit]]:
    """Product-less WorkOrder+PlanOperations+Units, one op per work center in
    `op_work_centers`, seq 1..N. Bypasses app.domain.workorders entirely --
    this test suite only needs the Phase-3 tables' shapes, not a real product."""
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=qty, **_mirror_common(),
    )
    db_session.add(line_item)
    db_session.flush()

    from app.domain.models_library import Product

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
            jb2_line_item_id=line_item.id, seq=seq, operation_code=f"OP{seq*10}",
            description=f"Op {seq}", work_center_code=wc, **_mirror_common(),
        )
        db_session.add(routing)
        db_session.flush()

        plan_op = PlanOperation(
            id=uuid.uuid4(), work_order_id=work_order.id, seq=seq,
            jb2_routing_id=routing.id, operation_code=routing.operation_code,
            title=f"Op {seq} ({wc})", frozen_content={"steps": []}, status="pending",
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
        db_session, name=name, work_center_code=work_center_code
    )
    db_session.commit()
    return station, token


def _make_operator(db_session, *, name="Alice", roles=None):
    operator = service.create_operator(db_session, display_name=name, roles=roles or ["operator"])
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


# -- S1/S2: badge scans ---------------------------------------------------------


def test_s1_badge_scan_opens_operator_session(client, db_session):
    station, token = _make_station(db_session)
    operator = _make_operator(db_session)

    resp = _scan(client, operator.badge_qr, token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "operator_session_opened"
    assert "mes_operator" in resp.cookies


def test_s2_unknown_badge_rejected(client, db_session):
    _, token = _make_station(db_session)

    resp = client.post(
        "/scan",
        json={"payload": "OP:00000000-0000-0000-0000-000000000000"},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == "badge_rejected"
    assert "mes_operator" not in resp.cookies


# -- S3: box scan, no operator session ------------------------------------------


def test_s3_box_scan_without_operator_required(client, db_session):
    station, token = _make_station(db_session)
    _, _, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])

    resp = _scan(client, box.qr_payload, token)
    assert resp.status_code == 200
    assert resp.json()["code"] == "operator_required"

    scans = db_session.execute(select(Scan)).scalars().all()
    assert len(scans) == 1
    assert scans[0].result == "rejected"
    assert db_session.execute(select(Event).where(Event.verb == "scan.rejected")).scalars().first()


# -- S4: unknown box -------------------------------------------------------------


def test_s4_unknown_box(client, db_session):
    station, token = _make_station(db_session)
    operator = _make_operator(db_session)
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = _scan(client, "BOX:does-not-exist", token)
    assert resp.json()["code"] == "unknown_box"
    scan = db_session.execute(select(Scan)).scalars().one()
    assert scan.result == "unknown_box"
    assert scan.box_id is None


# -- S5: unbound box --------------------------------------------------------------


def test_s5_unbound_box(client, db_session):
    station, token = _make_station(db_session)
    operator = _make_operator(db_session)
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    box = BuildBox(id=uuid.uuid4(), qr_payload=f"BOX:{uuid.uuid4()}", current_unit_id=None)
    db_session.add(box)
    db_session.commit()

    resp = _scan(client, box.qr_payload, token)
    assert resp.json()["code"] == "unbound_box"
    scan = db_session.execute(select(Scan)).scalars().one()
    assert scan.result == "unbound_box"
    assert scan.box_id == box.id


# -- S6: right-station accept -----------------------------------------------------


def test_s6_right_station_accept_opens_session(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = _scan(client, box.qr_payload, token)
    body = resp.json()
    assert body["code"] == "accepted"
    assert body["context"]["plan_operation_id"] == str(plan_ops[0].id)

    db_session.expire_all()
    unit = db_session.get(Unit, units[0].id)
    assert unit.status == "at_station"

    sessions = db_session.execute(select(WorkSession)).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].kind == "first_pass"

    scan = db_session.execute(select(Scan)).scalars().one()
    assert scan.result == "accepted"
    assert db_session.execute(select(Event).where(Event.verb == "scan.accepted")).scalars().first()
    assert db_session.execute(select(Event).where(Event.verb == "session.opened")).scalars().first()


def test_s6_closes_open_transit_hop(client, db_session):
    station_b, token_b = _make_station(db_session, name="B", work_center_code="ASSY1")
    operator = _make_operator(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    unit = units[0]
    box = _bind_box(db_session, unit)

    # unit already finished op 1 and is in_transit toward op 2 (ASSY1)
    unit.status = "in_transit"
    unit.current_plan_op_id = plan_ops[1].id
    departed = datetime.now(timezone.utc) - timedelta(seconds=90)
    db_session.add(Transit(id=uuid.uuid4(), unit_id=unit.id, from_station_id=None,
                            to_station_id=station_b.id, departed_at=departed, arrived_at=None))
    db_session.commit()

    _badge_in(client, badge_qr=operator.badge_qr, station_token=token_b)
    resp = _scan(client, box.qr_payload, token_b)
    assert resp.json()["code"] == "accepted"

    transit = db_session.execute(select(Transit)).scalars().one()
    assert transit.arrived_at is not None
    assert transit.seconds is not None and transit.seconds >= 90

    db_session.expire_all()
    assert db_session.get(Unit, unit.id).status == "at_station"


# -- S7: wrong station ------------------------------------------------------------


def test_s7_wrong_station_rejected(client, db_session):
    station, token = _make_station(db_session, work_center_code="ASSY1")  # unit's next op is CNC1
    operator = _make_operator(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = _scan(client, box.qr_payload, token)
    body = resp.json()
    assert body["code"] == "wrong_station"
    assert body["context"]["expected_work_center"] == "CNC1"

    scan = db_session.execute(select(Scan)).scalars().one()
    assert scan.result == "wrong_station"

    db_session.expire_all()
    assert db_session.get(Unit, units[0].id).status == "queued"  # unchanged


# -- O2: lead override on wrong station --------------------------------------------


def test_o2_lead_override_accepts_wrong_station(client, db_session):
    station, token = _make_station(db_session, work_center_code="ASSY1")
    operator = _make_operator(db_session, name="Op")
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    _, plan_ops, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = client.post(
        "/scan",
        json={"payload": box.qr_payload, "override_badge": lead.badge_qr},
        headers={"X-Station-Token": token},
    )
    body = resp.json()
    assert body["code"] == "accepted", body
    assert body["context"]["plan_operation_id"] == str(plan_ops[1].id)  # the ASSY1 op, out of seq

    scan = db_session.execute(select(Scan)).scalars().one()
    assert scan.override_by == lead.id


def test_o2_non_lead_override_rejected(client, db_session):
    station, token = _make_station(db_session, work_center_code="ASSY1")
    operator = _make_operator(db_session, name="Op")
    not_lead = _make_operator(db_session, name="NotLead", roles=["operator"])
    _, plan_ops, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = client.post(
        "/scan",
        json={"payload": box.qr_payload, "override_badge": not_lead.badge_qr},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 403

    db_session.expire_all()
    assert db_session.get(Unit, units[0].id).status == "queued"


# -- S8: terminal unit --------------------------------------------------------------


def test_s8_terminal_unit_rejected(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    units[0].status = "done"
    box = _bind_box(db_session, units[0])
    db_session.commit()
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    resp = _scan(client, box.qr_payload, token)
    assert resp.json()["code"] == "unit_terminal"
    assert db_session.execute(select(Scan)).scalars().one().result == "rejected"


# -- S9: busy elsewhere --------------------------------------------------------------


def test_s9_unit_busy_elsewhere_rejected(client, db_session):
    station_a, token_a = _make_station(db_session, name="A", work_center_code="CNC1")
    station_b, token_b = _make_station(db_session, name="B", work_center_code="CNC1")
    op_x = _make_operator(db_session, name="X")
    op_y = _make_operator(db_session, name="Y")
    _, plan_ops, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])

    _badge_in(client, badge_qr=op_x.badge_qr, station_token=token_a)
    r1 = _scan(client, box.qr_payload, token_a)
    assert r1.json()["code"] == "accepted"

    _badge_in(client, badge_qr=op_y.badge_qr, station_token=token_b)
    r2 = _scan(client, box.qr_payload, token_b)
    assert r2.json()["code"] == "unit_busy_elsewhere"


def test_s9_two_operators_same_station_allowed(client, db_session):
    """§17.7: two operators, same station, same unit -- both sessions open."""
    station, token = _make_station(db_session, work_center_code="CNC1")
    op_x = _make_operator(db_session, name="X")
    op_y = _make_operator(db_session, name="Y")
    _, plan_ops, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])

    _badge_in(client, badge_qr=op_x.badge_qr, station_token=token)
    r1 = _scan(client, box.qr_payload, token)
    assert r1.json()["code"] == "accepted"

    _badge_in(client, badge_qr=op_y.badge_qr, station_token=token)
    r2 = _scan(client, box.qr_payload, token)
    assert r2.json()["code"] == "accepted"

    assert db_session.execute(select(WorkSession)).scalars().all().__len__() == 2


# -- S10: duplicate scan idempotent --------------------------------------------------


def test_s10_duplicate_scan_is_idempotent(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    r1 = _scan(client, box.qr_payload, token)
    assert r1.json()["code"] == "accepted"

    r2 = _scan(client, box.qr_payload, token)
    assert r2.json()["code"] == "already_active"

    sessions = db_session.execute(select(WorkSession)).scalars().all()
    assert len(sessions) == 1  # no duplicate session opened

    scans = db_session.execute(select(Scan)).scalars().all()
    assert len(scans) == 2  # both scans audited
    assert all(s.result == "accepted" for s in scans)


# -- request_id replay --------------------------------------------------------------


def test_request_id_replay_is_side_effect_free(client, db_session):
    station, token = _make_station(db_session, work_center_code="CNC1")
    operator = _make_operator(db_session)
    _, plan_ops, units = _seed_work_order(db_session)
    box = _bind_box(db_session, units[0])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)

    rid = str(uuid.uuid4())
    r1 = client.post(
        "/scan", json={"payload": box.qr_payload, "request_id": rid},
        headers={"X-Station-Token": token},
    )
    r2 = client.post(
        "/scan", json={"payload": box.qr_payload, "request_id": rid},
        headers={"X-Station-Token": token},
    )
    assert r1.json() == r2.json()

    # only one scan/session/event triple -- the replay never re-ran the transition
    assert len(db_session.execute(select(Scan)).scalars().all()) == 1
    assert len(db_session.execute(select(WorkSession)).scalars().all()) == 1
    assert len(db_session.execute(select(RequestDedup)).scalars().all()) == 1
