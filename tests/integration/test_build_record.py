"""Integration tests for the per-serial build-record PDF (P4-08) --
DD §18.7/§9. Same zero-network sqlite-in-memory pattern as
tests/integration/test_finish_and_writeback.py (direct domain-fn seeding +
a trimmed FastAPI app for the one real HTTP hop this needs, `/operations/
{id}/finish`) and tests/integration/test_pdf.py (artifact-dir redirect via
dataclasses.replace, rendering the Jinja template directly to check
content pre-PDF).
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api import build_record as build_record_api
from app.api.auth import router as auth_router
from app.api.build_record import router as build_record_router
from app.api.operations import router as operations_router
from app.auth import service
from app.config import config as real_config
from app.db import get_session
from app.domain import statemachine
from app.domain import substeps as substeps_module
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import Event
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting
from app.domain.models_library import FailureCode, Product
from app.pdf import build_record as build_record_module

TEST_SECRET = "unit-test-secret-key-not-for-prod"


@pytest.fixture(autouse=True)
def _redirect_artifacts(tmp_path, monkeypatch):
    patched = dataclasses.replace(real_config, artifact_dir=str(tmp_path))
    monkeypatch.setattr(substeps_module, "config", patched)
    monkeypatch.setattr(build_record_module, "config", patched)
    monkeypatch.setattr(build_record_api, "config", patched)


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
    app.include_router(operations_router)
    app.include_router(build_record_router)
    return app, test_config


@pytest.fixture
def client(engine, monkeypatch, tmp_path):
    app, test_config = _build_app()
    monkeypatch.setattr("app.api.auth.config", test_config)
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


# -- seeding: one work order, one op, action + measurement + photo substeps ----


def _mirror_common() -> dict:
    return {"content_hash": "h", "jb2_last_modified": None, "synced_at": datetime.now(timezone.utc)}


def _seed_work_order_with_substeps(db_session: Session) -> tuple[WorkOrder, PlanOperation, Unit]:
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=1, due_date=date(2026, 8, 1),
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

    routing = JB2OrderRouting(
        id=uuid.uuid4(), jb2_id=f"routing-{work_order.id}", jb2_line_item_id=line_item.id,
        seq=10, operation_code="OP10", description="Final assembly", work_center_code="CNC1",
        payload={}, **_mirror_common(),
    )
    db_session.add(routing)
    db_session.flush()

    plan_op = PlanOperation(
        id=uuid.uuid4(), work_order_id=work_order.id, seq=1, jb2_routing_id=routing.id,
        operation_code="OP10", title="Final Assembly", instruction_version=3,
        frozen_content={
            "steps": [
                {
                    "seq": 1,
                    "title": "Assemble and inspect",
                    "substeps": [
                        {"seq": 1, "type": "action", "title": "Load fixture", "required": True},
                        {
                            "seq": 2, "type": "measurement", "title": "Verify chamber depth",
                            "required": True,
                            "measurement_spec": {
                                "name": "chamber_depth", "unit": "in", "nominal": 0.5,
                                "tol_plus": 0.01, "tol_minus": 0.01,
                            },
                        },
                        {"seq": 3, "type": "photo", "title": "Assembly photo", "required": True},
                    ],
                }
            ]
        },
        status="active",
    )
    db_session.add(plan_op)
    db_session.flush()

    unit = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=1, status="queued",
        first_pass=True, rework_count=0,
    )
    db_session.add(unit)
    db_session.flush()

    return work_order, plan_op, unit


def _make_station(db_session, *, work_center_code="CNC1"):
    station, token = service.create_station(
        db_session, name="CNC-1", work_center_code=work_center_code
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


def _walk_unit_to_done(db_session: Session, client) -> tuple[WorkOrder, PlanOperation, Unit, str]:
    """Domain-fn seeding through a measurement (out of tolerance ->
    use_as_is disposition -> Failure row), a photo attach, and the action
    substep, then the one real HTTP hop this needs -- the fixed finish
    contract `POST /operations/{id}/finish` -- to reach `done`."""
    station, token = _make_station(db_session)
    operator = _make_operator(db_session, name="Alice Operator")
    lead = _make_operator(db_session, name="Lead Bob", roles=["operator", "lead"])
    work_order, plan_op, unit = _seed_work_order_with_substeps(db_session)
    db_session.commit()

    statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_op,
        kind="first_pass",
    )
    db_session.commit()

    substeps_module.complete_substep(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=1,
    )
    db_session.commit()

    # out of tolerance: nominal 0.5 +/- 0.01, recorded 0.53
    substeps_module.record_measurement(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=2,
        value="0.53", gauge_id="CAL-9",
    )
    db_session.commit()

    failure_code = FailureCode(
        id=uuid.uuid4(), product_id=None, code="OOT-DEPTH", label="Chamber depth out of tolerance",
        active=True,
    )
    db_session.add(failure_code)
    db_session.commit()

    substeps_module.apply_disposition(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=2,
        disposition="use_as_is", failure_code_id=failure_code.id,
        notes="chamber depth 0.53in vs 0.50in nominal -- accepted per QA",
        authorizer=lead,
    )
    db_session.commit()

    substeps_module.attach_photo(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=3,
        data=b"\x89PNG\r\n\x1a\nfake-photo-bytes", ext="png",
    )
    db_session.commit()

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    unit.serial_number = "SN-BR-0001"
    db_session.commit()

    _badge_in(client, badge_qr=operator.badge_qr, station_token=token)
    resp = client.post(
        f"/operations/{plan_op.id}/finish", json={"unit_id": str(unit.id)},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["unit_status"] == "done"

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    assert unit.status == "done"
    assert unit.completed_at is not None

    return work_order, plan_op, unit, token


# -- 1. generated PDF contains the full record ---------------------------------


def test_generate_build_record_produces_pdf_with_full_record(db_session, client):
    _work_order, _plan_op, unit, _token = _walk_unit_to_done(db_session, client)

    path = build_record_module.generate_build_record(db_session, unit, generated_by="tester")
    assert path.is_file()
    pdf_bytes = path.read_bytes()
    assert pdf_bytes[:4] == b"%PDF"

    # pre-PDF rendered HTML, same trick as tests/integration/test_pdf.py
    ctx = build_record_module.build_record_context(db_session, unit, generated_by="tester")
    html_str = build_record_module._env.get_template("guide/build_record.html").render(**ctx)

    assert "SN-BR-0001" in html_str
    assert "0.53" in html_str  # recorded measurement value
    assert "OUT" in html_str  # out-of-tolerance flag
    assert "chamber depth 0.53in vs 0.50in nominal -- accepted per QA" in html_str
    assert "Alice Operator" in html_str  # operator who did the work
    assert "Lead Bob" in html_str  # authorizer
    assert "use_as_is" in html_str
    assert "first-pass" in html_str  # unit was never reworked in this walk


# -- 2. regeneration is idempotent (fixed path, no version growth) ------------


def test_regeneration_overwrites_same_path_with_same_recorded_content(db_session, client):
    _work_order, _plan_op, unit, _token = _walk_unit_to_done(db_session, client)

    path1 = build_record_module.generate_build_record(db_session, unit, generated_by="tester")
    ctx1 = build_record_module.build_record_context(db_session, unit, generated_by="tester")

    path2 = build_record_module.generate_build_record(db_session, unit, generated_by="tester")
    ctx2 = build_record_module.build_record_context(db_session, unit, generated_by="tester")

    # decision: fixed record-v1.pdf, overwritten -- no version-N growth like
    # plan_pdfs (see app/pdf/build_record.py module docstring).
    assert path1 == path2
    assert path1.name == "record-v1.pdf"

    # the *recorded* content (everything but the generation timestamp) is
    # identical across regenerations -- idempotent over the execution data.
    ctx1.pop("generated_at"), ctx2.pop("generated_at")
    assert ctx1 == ctx2


# -- 3. admin endpoints: generate + traversal-safe download --------------------


def test_admin_units_list_generate_and_download(db_session, client):
    _work_order, _plan_op, unit, _token = _walk_unit_to_done(db_session, client)
    unit_id = unit.id
    db_session.close()

    resp = client.get("/admin/units")
    assert resp.status_code == 200
    assert "SN-BR-0001" in resp.text

    resp2 = client.post(f"/admin/units/{unit_id}/build-record", follow_redirects=False)
    assert resp2.status_code == 303

    resp3 = client.get("/admin/units")
    assert "Download" in resp3.text

    download_url = "/artifacts/build-records/SN-BR-0001/record-v1.pdf"
    resp4 = client.get(download_url)
    assert resp4.status_code == 200
    assert resp4.content[:4] == b"%PDF"


def test_download_endpoint_rejects_path_traversal(tmp_path):
    """Calls the route function directly (not through TestClient/httpx,
    which may normalize `..` in a URL before it ever reaches our code) --
    this exercises the guard logic itself. `key=".."` is the attack
    `app/api/workorders.py`'s equivalent guard structurally can't have
    (its id segment is a typed `uuid.UUID`) but this endpoint's `key` is
    free text (a serial number), so it needs its own explicit check that
    `base` stays inside `build-records/`, not just that `path` stays
    inside `base`."""
    (tmp_path / "secret.txt").write_text("should never be servable")

    with pytest.raises(HTTPException) as exc_info:
        build_record_api.download_build_record(key="..", filename="secret.txt")
    assert exc_info.value.status_code == 404

    # legitimate lookup still works against the same tmp_path
    good_dir = tmp_path / "build-records" / "SN-OK"
    good_dir.mkdir(parents=True)
    (good_dir / "record-v1.pdf").write_bytes(b"%PDF-fake")
    resp = build_record_api.download_build_record(key="SN-OK", filename="record-v1.pdf")
    assert resp.status_code == 200


def test_build_record_endpoint_rejects_unit_not_done(db_session, client):
    _work_order, _plan_op, unit = _seed_work_order_with_substeps(db_session)
    db_session.commit()

    resp = client.post(f"/admin/units/{unit.id}/build-record")
    assert resp.status_code == 409
