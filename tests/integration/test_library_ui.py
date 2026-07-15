"""Integration tests for the instruction-library builder UI (P2-04/P2-05) —
DD §7.2, CR-003.

Same zero-network sqlite pattern as tests/integration/test_products.py.
Drives the server-rendered admin/library endpoints exactly as a browser
form-post would (PRG: POST then follow the 303 redirect), never touching
app/domain/library.py directly, since the point is coverage of the new
router + templates layer.
"""
from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import get_session
from app.domain.models_jb2 import Base
from app.domain.models_library import InstructionSet, Step, Substep
from app.main import app  # noqa: F401 -- registers library tables via import


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine):
    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app, follow_redirects=True) as c:
        yield c
    del app.dependency_overrides[get_session]


def _make_product(client, **kwargs):
    body = {"name": "Apollo", "variant_schema": {"caliber": ["9mm", ".45"]}}
    body.update(kwargs)
    resp = client.post("/api/v1/products", json=body)
    assert resp.status_code == 201
    return resp.json()


def _create_set(client, product_id="", title="Apollo Op40 Assembly"):
    resp = client.post(
        "/admin/library",
        data={
            "title": title,
            "product_id": product_id,
            "operation_match": '{"op_codes": ["OP40"], "work_centers": []}',
            "who": "alice",
        },
    )
    assert resp.status_code == 200  # followed the 303 to the set detail page
    m = re.search(r"/admin/library/sets/([0-9a-f-]{36})", str(resp.url))
    assert m, resp.url
    return uuid.UUID(m.group(1))


def _add_step(client, set_id, title, seq_hint=""):
    resp = client.post(
        f"/admin/library/sets/{set_id}/steps",
        data={"title": title, "body_html": "<b>bold</b> instructions", "who": "alice"},
    )
    assert resp.status_code == 200
    return resp


def test_author_3_step_6_substep_mixed_set(client, db_session):
    product = _make_product(client)
    set_id = _create_set(client, product_id=product["id"])

    for i in range(1, 4):
        _add_step(client, set_id, f"Step {i}")

    steps = db_session.scalars(
        select(Step).where(Step.instruction_set_id == set_id).order_by(Step.seq)
    ).all()
    assert [s.title for s in steps] == ["Step 1", "Step 2", "Step 3"]
    step1, step2 = steps[0], steps[1]

    # one substep per type, spread across the first two steps (6 total)
    substeps = [
        (step1.id, {"type": "action", "title": "Deburr edges", "body_html": "<b>careful</b>"}),
        (
            step1.id,
            {
                "type": "measurement",
                "title": "Slide width",
                "meas_name": "width",
                "meas_unit": "in",
                "meas_nominal": "0.750",
                "meas_tol_plus": "0.002",
                "meas_tol_minus": "-0.002",
                "meas_gauge_id": "CAL-01",
                "meas_decimals": "3",
            },
        ),
        (step1.id, {"type": "inspection", "title": "Visual check", "body_html": "no burrs"}),
        (step2.id, {"type": "photo", "title": "Photo of slide"}),
        (step2.id, {"type": "material", "title": "Record lot", "body_html": "lot number"}),
        (step2.id, {"type": "signoff", "title": "QC signoff", "signoff_role": "qc"}),
    ]
    for step_id, fields in substeps:
        resp = client.post(f"/admin/library/steps/{step_id}/substeps", data=fields)
        assert resp.status_code == 200, resp.text

    all_subs = db_session.scalars(select(Substep)).all()
    assert len(all_subs) == 6
    assert {s.type for s in all_subs} == {
        "action",
        "measurement",
        "inspection",
        "photo",
        "material",
        "signoff",
    }
    meas = next(s for s in all_subs if s.type == "measurement")
    assert meas.measurement_spec["nominal"] == 0.75
    assert meas.measurement_spec["decimals"] == 3
    signoff = next(s for s in all_subs if s.type == "signoff")
    assert signoff.signoff_role == "qc"


def test_reorder_step_persists(client, db_session):
    set_id = _create_set(client)
    _add_step(client, set_id, "Step A")
    _add_step(client, set_id, "Step B")

    steps = db_session.scalars(
        select(Step).where(Step.instruction_set_id == set_id).order_by(Step.seq)
    ).all()
    first_id = steps[0].id
    assert steps[0].title == "Step A"

    resp = client.post(f"/admin/library/steps/{first_id}/move", data={"direction": "down"})
    assert resp.status_code == 200

    db_session.expire_all()
    steps_after = db_session.scalars(
        select(Step).where(Step.instruction_set_id == set_id).order_by(Step.seq)
    ).all()
    assert [s.title for s in steps_after] == ["Step B", "Step A"]


def test_reorder_substep_persists(client, db_session):
    set_id = _create_set(client)
    _add_step(client, set_id, "Step 1")
    step = db_session.scalars(select(Step).where(Step.instruction_set_id == set_id)).first()

    client.post(
        f"/admin/library/steps/{step.id}/substeps",
        data={"type": "action", "title": "Sub A", "body_html": "a"},
    )
    client.post(
        f"/admin/library/steps/{step.id}/substeps",
        data={"type": "action", "title": "Sub B", "body_html": "b"},
    )
    subs = db_session.scalars(
        select(Substep).where(Substep.step_id == step.id).order_by(Substep.seq)
    ).all()
    assert [s.title for s in subs] == ["Sub A", "Sub B"]

    resp = client.post(f"/admin/library/substeps/{subs[0].id}/move", data={"direction": "down"})
    assert resp.status_code == 200

    db_session.expire_all()
    subs_after = db_session.scalars(
        select(Substep).where(Substep.step_id == step.id).order_by(Substep.seq)
    ).all()
    assert [s.title for s in subs_after] == ["Sub B", "Sub A"]


def test_publish_via_ui_and_edit_published_guard(client, db_session):
    set_id = _create_set(client)
    _add_step(client, set_id, "Step 1")

    resp = client.post(f"/admin/library/sets/{set_id}/submit", data={"who": "alice"})
    assert resp.status_code == 200
    resp = client.post(f"/admin/library/sets/{set_id}/publish", data={"who": "bob"})
    assert resp.status_code == 200

    db_session.expire_all()
    iset = db_session.get(InstructionSet, set_id)
    assert iset.state == "published"

    # editing a published set is rejected, and the guard error surfaces
    # inline on the redirected page (base.html renders `error` as a banner)
    resp = client.post(
        f"/admin/library/sets/{set_id}/steps",
        data={"title": "Should fail", "who": "alice"},
    )
    assert resp.status_code == 200
    assert "banner-error" in resp.text
    assert "draft/in_review" in resp.text


def test_bad_measurement_tolerance_rejected(client, db_session):
    set_id = _create_set(client)
    _add_step(client, set_id, "Step 1")
    step = db_session.scalars(select(Step).where(Step.instruction_set_id == set_id)).first()

    resp = client.post(
        f"/admin/library/steps/{step.id}/substeps",
        data={
            "type": "measurement",
            "title": "Bad tol",
            "meas_name": "width",
            "meas_nominal": "1.0",
            "meas_tol_plus": "-0.1",  # invalid: must be >= 0
            "meas_tol_minus": "-0.1",
            "meas_decimals": "2",
        },
    )
    assert resp.status_code == 200
    assert "banner-error" in resp.text
    assert "+tol" in resp.text
    assert db_session.scalars(select(Substep)).first() is None


def test_condition_validation_error_surfaces(client, db_session):
    product = _make_product(client)  # variant_schema: {"caliber": ["9mm", ".45"]}
    set_id = _create_set(client, product_id=product["id"])
    _add_step(client, set_id, "Step 1")
    step = db_session.scalars(select(Step).where(Step.instruction_set_id == set_id)).first()

    resp = client.post(
        f"/admin/library/steps/{step.id}/substeps",
        data={
            "type": "action",
            "title": "Conditional",
            "body_html": "x",
            "condition": '{"field": "unknown_field", "in": ["9mm"]}',
        },
    )
    assert resp.status_code == 200
    assert "banner-error" in resp.text
    assert "unknown variant field" in resp.text
    assert db_session.scalars(select(Substep)).first() is None


def test_sanitizer_strips_script_from_step_body(client, db_session):
    set_id = _create_set(client)
    resp = client.post(
        f"/admin/library/sets/{set_id}/steps",
        data={
            "title": "Step 1",
            "body_html": "<script>alert(1)</script><b>ok</b>",
            "who": "alice",
        },
    )
    assert resp.status_code == 200

    step = db_session.scalars(select(Step).where(Step.instruction_set_id == set_id)).first()
    assert "script" not in step.body_html
    assert "<b>ok</b>" in step.body_html


def test_preview_renders_all_six_substep_types(client, db_session):
    set_id = _create_set(client)
    _add_step(client, set_id, "Step 1")
    step = db_session.scalars(select(Step).where(Step.instruction_set_id == set_id)).first()

    for fields in (
        {"type": "action", "title": "Action sub", "body_html": "do it"},
        {
            "type": "measurement",
            "title": "Meas sub",
            "meas_name": "width",
            "meas_nominal": "1.0",
            "meas_tol_plus": "0.1",
            "meas_tol_minus": "-0.1",
            "meas_decimals": "2",
        },
        {"type": "inspection", "title": "Inspect sub", "body_html": "check it"},
        {"type": "photo", "title": "Photo sub"},
        {"type": "material", "title": "Material sub", "body_html": "record lot"},
        {"type": "signoff", "title": "Signoff sub", "signoff_role": "lead"},
    ):
        resp = client.post(f"/admin/library/steps/{step.id}/substeps", data=fields)
        assert resp.status_code == 200, resp.text

    resp = client.get(f"/admin/library/sets/{set_id}/preview")
    assert resp.status_code == 200
    for title in (
        "Action sub",
        "Meas sub",
        "Inspect sub",
        "Photo sub",
        "Material sub",
        "Signoff sub",
    ):
        assert title in resp.text


def test_diff_view_returns_200(client, db_session):
    set_id = _create_set(client)
    _add_step(client, set_id, "Step 1")
    client.post(f"/admin/library/sets/{set_id}/submit", data={"who": "alice"})
    client.post(f"/admin/library/sets/{set_id}/publish", data={"who": "bob"})

    resp = client.post(f"/admin/library/sets/{set_id}/new-draft", data={"who": "carol"})
    m = re.search(r"/admin/library/sets/([0-9a-f-]{36})", str(resp.url))
    v2_id = m.group(1)

    resp = client.get(f"/admin/library/sets/{set_id}/diff/{v2_id}")
    assert resp.status_code == 200


def test_library_index_lists_created_set(client):
    set_id = _create_set(client, title="Listed Set")
    resp = client.get("/admin/library")
    assert resp.status_code == 200
    assert "Listed Set" in resp.text
    assert str(set_id) in resp.text
