"""Integration tests for the station kiosk (P3-05/06/07/12) -- DD §12,
docs/state-machine.md §5. Zero-network sqlite, same pattern as
tests/integration/test_scan.py: a standalone FastAPI app (the real
auth/scan/station/substeps routers) against a fresh in-memory engine,
domain-level seeding (WorkOrder/PlanOperation/Unit/BuildBox inserted
directly, bypassing app.domain.workorders).

Walks one unit through a full operation: badge in, scan the box (S6),
work every substep type (action, measurement in-tolerance, measurement
out-of-tolerance blocked-until-disposition, photo, material, signoff with
a second badge, skip with a lead second badge -- O1), pause/resume, and the
finish-gating count (P3-10's actual finish endpoint is a different task;
here we only assert the Finish button's disabled/remaining-count render,
which is this task's own `substeps.remaining_required_count`)."""
from __future__ import annotations

import dataclasses
import io
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
    Attachment,
    BoxAssignment,
    BuildBox,
    Event,
    MaterialRecord,
    Measurement,
    SessionPause,
    SubstepExecution,
    WorkSession,
)
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting

# Registers the full model graph on Base.metadata (work_orders etc.).
from app.main import app as _main_app  # noqa: F401

TEST_SECRET = "unit-test-secret-key-not-for-prod"

MEASUREMENT_SPEC = {
    "nominal": 1.0, "tol_plus": 0.005, "tol_minus": 0.005, "unit": "in", "decimals": 3,
}


def _sub(seq, type_, title, *, spec=None, signoff_role=None):
    return {
        "seq": seq, "type": type_, "title": title, "body_html": None, "required": True,
        "measurement_spec": spec, "media": [], "signoff_role": signoff_role,
    }


FROZEN_STEP = {
    "seq": 1,
    "title": "Assemble slide",
    "body_html": "<p>Assemble per drawing.</p>",
    "est_minutes": 12,
    "substeps": [
        _sub(1, "action", "Clean part"),
        _sub(2, "measurement", "Bore diameter", spec=MEASUREMENT_SPEC),
        _sub(3, "photo", "Photo of assembly"),
        _sub(4, "material", "Record grease used"),
        _sub(5, "signoff", "QC signoff", signoff_role="lead"),
        _sub(6, "action", "Optional cosmetic pass"),
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


def _mirror_common() -> dict:
    return {"payload": {}, "content_hash": "h", "jb2_last_modified": None,
            "synced_at": datetime.now(timezone.utc)}


def _seed(db_session: Session) -> tuple[WorkOrder, PlanOperation, Unit, BuildBox]:
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=1, **_mirror_common(),
    )
    db_session.add(line_item)
    db_session.flush()

    from app.domain.models_library import Product

    product = Product(id=uuid.uuid4(), name="Apollo", variant_schema={}, active=True)
    db_session.add(product)
    db_session.flush()

    work_order = WorkOrder(
        id=uuid.uuid4(), jb2_line_item_id=line_item.id, product_id=product.id,
        qty=1, status="in_progress",
    )
    db_session.add(work_order)
    db_session.flush()

    routing = JB2OrderRouting(
        id=uuid.uuid4(), jb2_id=f"routing-{work_order.id}-1", jb2_line_item_id=line_item.id,
        seq=1, operation_code="OP10", description="Assemble", work_center_code="ASSY1",
        **_mirror_common(),
    )
    db_session.add(routing)
    db_session.flush()

    plan_op = PlanOperation(
        id=uuid.uuid4(), work_order_id=work_order.id, seq=1, jb2_routing_id=routing.id,
        operation_code="OP10", title="Assemble slide", frozen_content={
            "instruction_set_id": str(uuid.uuid4()), "version": 1, "steps": [FROZEN_STEP],
        },
        status="pending", est_minutes=12, blocked=False,
    )
    db_session.add(plan_op)
    db_session.flush()

    unit = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=1, status="queued",
        first_pass=True, rework_count=0,
    )
    db_session.add(unit)
    db_session.flush()

    box = BuildBox(id=uuid.uuid4(), qr_payload=f"BOX:{uuid.uuid4()}", current_unit_id=unit.id)
    db_session.add(box)
    db_session.flush()
    db_session.add(BoxAssignment(id=uuid.uuid4(), box_id=box.id, unit_id=unit.id))
    db_session.flush()

    return work_order, plan_op, unit, box


def _make_station(db_session, *, work_center_code="ASSY1"):
    station, token = service.create_station(
        db_session, name="Assy 1", work_center_code=work_center_code
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


def _substep_url(unit_id, step_seq, sub_seq, action):
    return f"/station/units/{unit_id}/substeps/{step_seq}/{sub_seq}/{action}"


def _current_sub(db_session, seq):
    """The non-superseded SubstepExecution for `seq` (append-only rework
    history means a substep_seq can have more than one row over time)."""
    return db_session.execute(
        select(SubstepExecution).where(
            SubstepExecution.substep_seq == seq, SubstepExecution.superseded.is_(False)
        )
    ).scalars().one()


@pytest.fixture
def walk(client, db_session):
    """Shared setup for the substep-walk tests: station/operator/lead badged
    in, box scanned (S6 accept), execute screen reachable."""
    work_order, plan_op, unit, box = _seed(db_session)
    station, token = _make_station(db_session)
    operator = _make_operator(db_session, name="Op1")
    lead = _make_operator(db_session, name="Lead1", roles=["operator", "lead"])
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    _scan_box(client, box_payload=box.qr_payload, station_token=token)
    return {
        "work_order": work_order, "plan_op": plan_op, "unit": unit, "box": box,
        "station": station, "token": token, "operator": operator, "lead": lead,
    }


HEADERS = lambda token: {"X-Station-Token": token}  # noqa: E731


# -- full substep walk ------------------------------------------------------------


def test_execute_screen_renders_with_all_substeps(client, walk):
    resp = client.get(f"/station/execute/{walk['unit'].id}", headers=HEADERS(walk["token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "Assemble slide" in body
    assert "Clean part" in body
    assert "Bore diameter" in body
    assert "width=device-width" in body  # CR-009 mobile-first viewport meta
    assert "6 required step" in body or "remaining" in body  # nothing done yet
    assert "disabled" in body  # Finish button gated


def test_action_substep_complete(client, walk, db_session):
    db_session.expire_all()
    resp = client.post(
        _substep_url(walk["unit"].id, 1, 1, "complete"), data={"request_id": "r-action-1"},
        headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    sub = db_session.execute(
        select(SubstepExecution).where(SubstepExecution.substep_seq == 1)
    ).scalars().one()
    assert sub.status == "done"
    assert sub.completed_at is not None
    assert db_session.execute(
        select(Event).where(Event.verb == "substep.done")
    ).scalars().first() is not None


def test_measurement_out_of_tolerance_blocks_then_disposition_unblocks(client, walk, db_session):
    db_session.expire_all()
    unit_id = walk["unit"].id
    # out of tolerance
    resp = client.post(
        _substep_url(unit_id, 1, 2, "measurement"),
        data={"value": "1.500", "request_id": "r-meas-1"}, headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    sub = _current_sub(db_session, 2)
    assert sub.status == "failed"
    assert sub.out_of_tolerance is True
    assert sub.disposition is None
    measurement = db_session.execute(select(Measurement)).scalars().one()
    assert measurement.in_tolerance is False

    # remaining count still counts this substep as open
    from app.domain import substeps as substeps_domain
    remaining_before = substeps_domain.remaining_required_count(
        db_session, walk["plan_op"], walk["unit"]
    )
    assert remaining_before >= 1

    # disposition: rework-here (no second badge needed) -> resets to pending
    resp = client.post(
        _substep_url(unit_id, 1, 2, "disposition"),
        data={"disposition": "rework_in_place", "request_id": "r-disp-1"},
        headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    old_sub = db_session.get(SubstepExecution, sub.id)
    assert old_sub.superseded is True
    assert old_sub.disposition == "rework_in_place"
    new_sub = _current_sub(db_session, 2)
    assert new_sub.status == "pending"
    unit = db_session.get(Unit, unit_id)
    assert unit.first_pass is False
    assert unit.rework_count == 1

    # re-measure in tolerance -> done
    resp = client.post(
        _substep_url(unit_id, 1, 2, "measurement"),
        data={"value": "1.000", "request_id": "r-meas-2"}, headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    final_sub = _current_sub(db_session, 2)
    assert final_sub.status == "done"
    assert final_sub.out_of_tolerance is False

    assert db_session.execute(
        select(Event).where(Event.verb == "measurement.recorded")
    ).scalars().all().__len__() == 2
    assert db_session.execute(
        select(Event).where(Event.verb == "disposition.applied")
    ).scalars().first() is not None
    assert db_session.execute(
        select(Event).where(Event.verb == "unit.reworked")
    ).scalars().first() is not None


def test_photo_attach(client, walk, db_session):
    db_session.expire_all()
    files = {"file": ("test.png", io.BytesIO(b"\x89PNG\r\n\x1a\nfakepngbytes"), "image/png")}
    resp = client.post(
        _substep_url(walk["unit"].id, 1, 3, "photo"), data={"request_id": "r-photo-1"},
        files=files, headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    sub = db_session.execute(
        select(SubstepExecution).where(SubstepExecution.substep_seq == 3)
    ).scalars().one()
    assert sub.status == "done"
    attachment = db_session.execute(select(Attachment)).scalars().one()
    assert attachment.entity_id == sub.id
    assert attachment.kind == "photo"


def test_material_record(client, walk, db_session):
    db_session.expire_all()
    resp = client.post(
        _substep_url(walk["unit"].id, 1, 4, "material"),
        data={
            "part_number": "GRS-100", "qty_used": "2.5", "qty_scrapped": "0",
            "request_id": "r-mat-1",
        },
        headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    sub = db_session.execute(
        select(SubstepExecution).where(SubstepExecution.substep_seq == 4)
    ).scalars().one()
    assert sub.status == "done"
    material = db_session.execute(select(MaterialRecord)).scalars().one()
    assert material.part_number == "GRS-100"
    assert float(material.qty_used) == 2.5


def test_signoff_requires_second_badge(client, walk, db_session):
    db_session.expire_all()
    # wrong role (operator, not lead) rejected
    resp = client.post(
        _substep_url(walk["unit"].id, 1, 5, "signoff"),
        data={"override_badge": walk["operator"].badge_qr, "request_id": "r-signoff-bad"},
        headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    db_session.expire_all()
    sub = db_session.execute(
        select(SubstepExecution).where(SubstepExecution.substep_seq == 5)
    ).scalars().first()
    assert sub is None or sub.status != "done"

    # correct lead badge accepted
    resp = client.post(
        _substep_url(walk["unit"].id, 1, 5, "signoff"),
        data={"override_badge": walk["lead"].badge_qr, "request_id": "r-signoff-ok"},
        headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    sub = db_session.execute(
        select(SubstepExecution).where(SubstepExecution.substep_seq == 5)
    ).scalars().one()
    assert sub.status == "done"
    assert sub.operator_id == walk["lead"].id


def test_skip_requires_lead_badge_o1(client, walk, db_session):
    db_session.expire_all()
    # wrong role (operator, not lead) -> rejected
    resp = client.post(
        _substep_url(walk["unit"].id, 1, 6, "skip"),
        data={
            "skip_reason": "n/a", "override_badge": walk["operator"].badge_qr,
            "request_id": "r-skip-bad",
        },
        headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    resp = client.post(
        _substep_url(walk["unit"].id, 1, 6, "skip"),
        data={
            "skip_reason": "cosmetic not required", "override_badge": walk["lead"].badge_qr,
            "request_id": "r-skip-ok",
        },
        headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    sub = db_session.execute(
        select(SubstepExecution).where(SubstepExecution.substep_seq == 6)
    ).scalars().one()
    assert sub.status == "skipped"
    assert sub.skip_authorized_by == walk["lead"].id
    assert db_session.execute(
        select(Event).where(Event.verb == "substep.skipped")
    ).scalars().first() is not None


def test_step_completion_gates_finish_button(client, walk, db_session):
    db_session.expire_all()
    unit_id = walk["unit"].id
    token = walk["token"]
    # complete everything: action(1), measurement(2) in-tolerance, photo(3),
    # material(4), signoff(5) w/ lead, skip(6) w/ lead
    client.post(
        _substep_url(unit_id, 1, 1, "complete"), data={"request_id": "g1"}, headers=HEADERS(token)
    )
    client.post(
        _substep_url(unit_id, 1, 2, "measurement"), data={"value": "1.000", "request_id": "g2"},
        headers=HEADERS(token),
    )
    files = {"file": ("t.png", io.BytesIO(b"\x89PNGdata"), "image/png")}
    client.post(
        _substep_url(unit_id, 1, 3, "photo"), data={"request_id": "g3"},
        files=files, headers=HEADERS(token),
    )
    client.post(
        _substep_url(unit_id, 1, 4, "material"),
        data={"part_number": "GRS-100", "qty_used": "1", "request_id": "g4"},
        headers=HEADERS(token),
    )

    # not yet all done -- Finish should still be disabled
    resp = client.get(f"/station/execute/{unit_id}", headers=HEADERS(token))
    assert "disabled" in resp.text

    client.post(
        _substep_url(unit_id, 1, 5, "signoff"),
        data={"override_badge": walk["lead"].badge_qr, "request_id": "g5"}, headers=HEADERS(token),
    )
    client.post(
        _substep_url(unit_id, 1, 6, "skip"),
        data={"skip_reason": "n/a", "override_badge": walk["lead"].badge_qr, "request_id": "g6"},
        headers=HEADERS(token),
    )

    db_session.expire_all()
    from app.domain import substeps as substeps_domain
    remaining = substeps_domain.remaining_required_count(db_session, walk["plan_op"], walk["unit"])
    assert remaining == 0

    resp = client.get(f"/station/execute/{unit_id}", headers=HEADERS(token))
    assert resp.status_code == 200
    assert "disabled" not in resp.text
    assert "Finish operation</button>" in resp.text or "Finish operation\n" in resp.text


# -- pause/resume -------------------------------------------------------------------


def test_pause_and_resume(client, walk, db_session):
    ws = db_session.execute(select(WorkSession)).scalars().one()
    resp = client.post(
        f"/station/sessions/{ws.id}/pause", data={"reason_code": "break"},
        headers=HEADERS(walk["token"]),
    )
    assert resp.status_code == 303
    db_session.expire_all()
    pause = db_session.execute(select(SessionPause)).scalars().one()
    assert pause.ended_at is None
    assert db_session.execute(
        select(Event).where(Event.verb == "session.paused")
    ).scalars().first() is not None

    resp = client.post(f"/station/sessions/{ws.id}/resume", headers=HEADERS(walk["token"]))
    assert resp.status_code == 303
    db_session.expire_all()
    pause = db_session.execute(select(SessionPause)).scalars().one()
    assert pause.ended_at is not None
    assert db_session.execute(
        select(Event).where(Event.verb == "session.resumed")
    ).scalars().first() is not None


# -- session-must-be-open validation ------------------------------------------------


def test_substep_action_rejected_without_open_session(client, db_session):
    """Same station/operator but no scan yet -- no open work session, so the
    substep endpoint must reject (contract §5 "session-must-be-open-by-actor")."""
    work_order, plan_op, unit, box = _seed(db_session)
    station, token = _make_station(db_session)
    operator = _make_operator(db_session)
    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    # no box scan -- no WorkSession opened
    resp = client.post(
        _substep_url(unit.id, 1, 1, "complete"), data={"request_id": "r-no-session"},
        headers=HEADERS(token),
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    assert db_session.execute(select(SubstepExecution)).scalars().first() is None


# -- UI smoke: idle + execute pages render 200 with expected strings ---------------


def test_idle_screen_smoke(client, db_session):
    station, token = _make_station(db_session, work_center_code="ASSY1")
    resp = client.get("/station", headers=HEADERS(token))
    assert resp.status_code == 200
    assert "Assy 1" in resp.text
    assert "Scan your badge" in resp.text
    assert "width=device-width" in resp.text


def test_idle_screen_shows_queued_unit(client, db_session):
    _seed(db_session)
    station, token = _make_station(db_session, work_center_code="ASSY1")
    resp = client.get("/station", headers=HEADERS(token))
    assert resp.status_code == 200
    assert "Apollo" in resp.text
    assert "Assemble slide" in resp.text


def test_execute_screen_smoke_at_tablet_viewport(client, walk):
    # "tablet viewport" here means: server-rendered HTML asserted directly
    # (no browser/Playwright per this task's AC) -- the meta viewport tag
    # and 48px-target CSS classes are what make the *rendered* page correct
    # at that width; content assertions stand in for a visual check.
    resp = client.get(f"/station/execute/{walk['unit'].id}", headers=HEADERS(walk["token"]))
    assert resp.status_code == 200
    assert "maximum-scale=1" in resp.text
    assert "exec-shell" in resp.text
    assert "exec-footer" in resp.text
