"""Integration tests for app/domain/boxes.py + app/api/boxes.py (P3-11),
docs/state-machine.md §8 (kit-up) and §17.6 (box reassignment/lost box).

Same domain-level seeding pattern as tests/integration/test_scan.py: a
minimal jb2_order_line_item + WorkOrder/PlanOperation/Unit satisfy the FK
graph directly, bypassing app.domain.workorders.
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
from app.api.boxes import router as boxes_router
from app.api.scan import router as scan_router
from app.auth import service
from app.config import config as real_config
from app.db import get_session
from app.domain import boxes
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import BoxAssignment, BuildBox, Event
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting

# Registers the full model graph on Base.metadata (work_orders etc.).
from app.main import app as _main_app  # noqa: F401

TEST_SECRET = "unit-test-secret-key-not-for-prod"


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    # ponytail: same sqlite bigint-autoincrement workaround as test_scan.py.
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
    app.include_router(boxes_router)
    return app, test_config


@pytest.fixture
def app_and_config():
    return _build_app()


@pytest.fixture
def client(engine, app_and_config, monkeypatch):
    app, test_config = app_and_config
    monkeypatch.setattr("app.api.auth.config", test_config)
    monkeypatch.setattr("app.api.scan.config", test_config)
    monkeypatch.setattr("app.api.boxes.config", test_config)
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
    db_session: Session, *, qty: int = 1, status: str = "ready",
    op_work_centers: tuple[str, ...] = ("CNC1",),
) -> tuple[WorkOrder, list[PlanOperation], list[Unit]]:
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
        qty=qty, status=status,
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


def _make_operator(db_session, *, name="Alice", roles=None):
    operator = service.create_operator(db_session, display_name=name, roles=roles or ["operator"])
    db_session.commit()
    return operator


def _make_station(db_session, *, name="Station", work_center_code="CNC1"):
    station, token = service.create_station(
        db_session, name=name, work_center_code=work_center_code
    )
    db_session.commit()
    return station, token


# -- register_box -------------------------------------------------------------------


def test_register_box_idempotent(db_session):
    payload = f"BOX:{uuid.uuid4()}"
    b1 = boxes.register_box(db_session, payload, label="B-1")
    b2 = boxes.register_box(db_session, payload, label="ignored-second-label")
    db_session.commit()

    assert b1.id == b2.id
    assert b2.label == "B-1"  # first registration wins
    assert len(db_session.execute(select(BuildBox)).scalars().all()) == 1


# -- assign_box happy path + end-to-end via /scan ------------------------------------


def test_assign_box_happy_path_flips_wo_and_unblocks_scan(client, db_session):
    wo, plan_ops, units = _seed_work_order(db_session, status="ready", op_work_centers=("CNC1",))
    unit = units[0]
    operator = _make_operator(db_session)
    station, token = _make_station(db_session, work_center_code="CNC1")

    box_payload = f"BOX:{uuid.uuid4()}"
    box = boxes.register_box(db_session, box_payload)
    db_session.commit()

    resp = client.post(
        "/auth/badge", json={"payload": operator.badge_qr}, headers={"X-Station-Token": token}
    )
    assert resp.status_code == 200

    # S5: box exists but isn't bound to any unit yet
    r_unbound = client.post(
        "/scan", json={"payload": box_payload}, headers={"X-Station-Token": token}
    )
    assert r_unbound.json()["code"] == "unbound_box"

    # kit-up: assign the box to the unit, entering its pre-existing serial (CR-007)
    boxes.assign_box(db_session, box, unit, operator, "SN-100")
    db_session.commit()

    db_session.expire_all()
    assert db_session.get(WorkOrder, wo.id).status == "in_progress"  # §8: WO -> in_progress
    assert db_session.get(Unit, unit.id).serial_number == "SN-100"
    assert db_session.get(BuildBox, box.id).current_unit_id == unit.id

    # S6: now that it's bound, the same box scan is accepted at the right station
    r_accept = client.post(
        "/scan", json={"payload": box_payload}, headers={"X-Station-Token": token}
    )
    assert r_accept.json()["code"] == "accepted"

    verbs = db_session.execute(select(Event.verb)).scalars().all()
    assert "box.assigned" in verbs
    assert "wo.status_changed" in verbs


def test_assign_box_rejects_when_wo_not_ready(db_session):
    wo, _, units = _seed_work_order(db_session, status="pending_sync")
    operator = _make_operator(db_session)
    box = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")

    with pytest.raises(boxes.BoxError):
        boxes.assign_box(db_session, box, units[0], operator)


def test_assign_box_rejects_unit_that_already_has_a_box(db_session):
    wo, _, units = _seed_work_order(db_session, status="ready")
    operator = _make_operator(db_session)
    box_a = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")
    box_b = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")

    boxes.assign_box(db_session, box_a, units[0], operator)
    db_session.commit()

    with pytest.raises(boxes.BoxError, match="already has a box"):
        boxes.assign_box(db_session, box_b, units[0], operator)


# -- serial uniqueness + set_serial ---------------------------------------------------


def test_serial_uniqueness_and_set_serial_rules(db_session):
    _, _, units = _seed_work_order(db_session, qty=2, status="ready")
    u1, u2 = units
    operator = _make_operator(db_session)
    box1 = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")
    box2 = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")

    boxes.assign_box(db_session, box1, u1, operator, "SN-1")
    db_session.commit()

    # duplicate serial at kit-up time is rejected with a friendly error
    with pytest.raises(boxes.BoxError, match="already recorded"):
        boxes.assign_box(db_session, box2, u2, operator, "SN-1")
    db_session.rollback()

    boxes.assign_box(db_session, box2, u2, operator)  # no serial yet
    db_session.commit()

    # set_serial: unique-when-set is enforced here too
    with pytest.raises(boxes.BoxError, match="already recorded"):
        boxes.set_serial(db_session, u2, "SN-1", operator)
    db_session.rollback()

    boxes.set_serial(db_session, u2, "SN-2", operator)
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Unit, u2.id).serial_number == "SN-2"

    # set_serial on a done unit is rejected
    unit2 = db_session.get(Unit, u2.id)
    unit2.status = "done"
    db_session.commit()
    with pytest.raises(boxes.BoxError, match="completed unit"):
        boxes.set_serial(db_session, unit2, "SN-3", operator)


# -- reassign preserves history --------------------------------------------------------


def test_reassign_box_preserves_history(db_session):
    _, _, units = _seed_work_order(db_session, status="ready")
    unit = units[0]
    operator = _make_operator(db_session, name="Op")
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    old_box = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")
    new_box = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")

    boxes.assign_box(db_session, old_box, unit, operator, "SN-9")
    db_session.commit()

    boxes.reassign_box(db_session, old_box, new_box, unit, lead, retire_old=True)
    db_session.commit()

    db_session.expire_all()
    assignments = db_session.execute(
        select(BoxAssignment)
        .where(BoxAssignment.unit_id == unit.id)
        .order_by(BoxAssignment.assigned_at)
    ).scalars().all()
    assert len(assignments) == 2
    assert assignments[0].box_id == old_box.id and assignments[0].released_at is not None
    assert assignments[1].box_id == new_box.id and assignments[1].released_at is None

    old = db_session.get(BuildBox, old_box.id)
    new = db_session.get(BuildBox, new_box.id)
    assert old.active is False  # retire_old
    assert old.current_unit_id is None
    assert new.current_unit_id == unit.id

    assert db_session.get(Unit, unit.id).serial_number == "SN-9"  # carried forward, untouched

    verbs = db_session.execute(select(Event.verb)).scalars().all()
    assert verbs.count("box.assigned") == 2  # original kit-up + reassign
    assert "box.released" in verbs
    assert "unit.moved" in verbs


def test_reassign_box_rejects_wrong_unit(db_session):
    _, _, units = _seed_work_order(db_session, qty=2, status="ready")
    operator = _make_operator(db_session)
    lead = _make_operator(db_session, name="Lead", roles=["lead"])
    old_box = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")
    new_box = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")

    boxes.assign_box(db_session, old_box, units[0], operator)
    db_session.commit()

    with pytest.raises(boxes.BoxError, match="not currently assigned"):
        boxes.reassign_box(db_session, old_box, new_box, units[1], lead)


# -- admin UI: kitup page + KITUP_REQUIRES_LEAD ---------------------------------------


def test_kitup_page_renders(client, db_session):
    _seed_work_order(db_session, status="ready")
    resp = client.get("/admin/kitup")
    assert resp.status_code == 200
    assert "Kit-up" in resp.text


def test_kitup_requires_lead_blocks_non_lead(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "app.api.boxes.config",
        dataclasses.replace(real_config, mes_secret_key=TEST_SECRET, kitup_requires_lead=True),
    )
    wo, _, units = _seed_work_order(db_session, status="ready")
    unit = units[0]
    non_lead = _make_operator(db_session, name="NotLead", roles=["operator"])

    resp = client.post(
        "/admin/kitup",
        data={
            "work_order_id": str(wo.id),
            "unit_id": str(unit.id),
            "box_qr": f"BOX:{uuid.uuid4()}",
            "serial": "",
            "operator_badge": non_lead.badge_qr,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "lead" in resp.headers["location"]

    db_session.expire_all()
    assert not db_session.execute(select(BuildBox)).scalars().all()  # nothing registered
    assert boxes.boxless_units(db_session, wo.id) == [unit]  # unit still boxless

    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    resp2 = client.post(
        "/admin/kitup",
        data={
            "work_order_id": str(wo.id),
            "unit_id": str(unit.id),
            "box_qr": f"BOX:{uuid.uuid4()}",
            "serial": "",
            "operator_badge": lead.badge_qr,
        },
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    assert "success" in resp2.headers["location"]
    db_session.expire_all()
    assert boxes.boxless_units(db_session, wo.id) == []


def test_boxes_page_renders(client, db_session):
    _, _, units = _seed_work_order(db_session, status="ready")
    operator = _make_operator(db_session)
    box = boxes.register_box(db_session, f"BOX:{uuid.uuid4()}")
    boxes.assign_box(db_session, box, units[0], operator)
    db_session.commit()

    resp = client.get("/admin/boxes")
    assert resp.status_code == 200
    assert "Boxes" in resp.text
