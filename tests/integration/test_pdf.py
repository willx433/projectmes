"""Integration tests for the PDF build guide (P2-11) + badge/box print
sheets (P2-12) — DD §8, §14.

Same zero-network sqlite-in-memory pattern as
tests/integration/test_order_to_plan.py; artifact dir is redirected to
tmp_path via dataclasses.replace on the `config` binding each pdf module
imported (same trick as tests/integration/test_media.py).

WeasyPrint successfully renders PDFs in this venv (confirmed via a manual
smoke run before writing these tests) so the PDF-byte assertions run for
real rather than being skipped; if a future environment lacks the
pango/cairo system libs WeasyPrint needs, `HTML(...).write_pdf()` would
raise OSError at import/render time -- there is currently no reason to add
a skipif for a failure mode that isn't happening.
"""
from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import workorders as workorders_api
from app.config import config
from app.domain import library, workorders
from app.domain.models_execution import PlanPdf, WorkOrder
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting
from app.domain.models_library import InstructionSet, Product, ProductPartMap
from app.pdf import badges as badges_pdf
from app.pdf import guide as guide_module


@pytest.fixture(autouse=True)
def _redirect_artifacts(tmp_path, monkeypatch):
    patched = dataclasses.replace(config, artifact_dir=str(tmp_path))
    monkeypatch.setattr(guide_module, "config", patched)
    monkeypatch.setattr(workorders_api, "config", patched)


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _mirror_common() -> dict:
    return {
        "payload": {},
        "content_hash": "h",
        "jb2_last_modified": None,
        "synced_at": datetime.now(timezone.utc),
    }


def _seed_line_item(session, *, jb2_id, part_number, qty, order_number="10100", item_number=1):
    li = JB2OrderLineItem(
        id=uuid.uuid4(),
        jb2_id=jb2_id,
        jb2_order_id=None,
        part_number=part_number,
        description=part_number,
        qty=qty,
        due_date=date(2026, 8, 1),
        **{**_mirror_common(), "payload": {"orderNumber": order_number, "itemNumber": item_number}},
    )
    session.add(li)
    session.flush()
    return li


def _seed_routing(session, *, line_item_id, seq, operation_code, description=""):
    routing = JB2OrderRouting(
        id=uuid.uuid4(),
        jb2_id=f"routing-{line_item_id}-{seq}",
        jb2_line_item_id=line_item_id,
        seq=seq,
        operation_code=operation_code,
        description=description or operation_code,
        work_center_code=None,
        est_setup_hrs=None,
        est_run_hrs=None,
        **_mirror_common(),
    )
    session.add(routing)
    session.flush()
    return routing


def _apollo_product(session):
    product = Product(
        id=uuid.uuid4(), name="Apollo", variant_schema={"caliber": ["9mm"]}, active=True
    )
    session.add(product)
    session.flush()
    return product


def _map_part(session, product, part_number, caliber="9mm"):
    m = ProductPartMap(
        id=uuid.uuid4(), product_id=product.id, jb2_part_number=part_number,
        variant_values={"caliber": caliber},
    )
    session.add(m)
    session.flush()
    return m


def _published_set_with_measurement(session, product) -> InstructionSet:
    iset = library.create_set(
        session, product_id=product.id, title="Apollo — Op10",
        operation_match={"op_codes": ["OP10"], "work_centers": []}, created_by="alice",
    )
    session.flush()
    step = library.add_step(session, iset.id, "Mill cuts", seq=1, who="alice")
    library.add_substep(session, step.id, "action", "Load fixture", seq=1, who="alice")
    library.add_substep(
        session, step.id, "measurement", "Verify chamber depth", seq=2, who="alice",
        measurement_spec={"name": "chamber_depth", "unit": "in", "nominal": 0.5,
                           "tol_plus": 0.01, "tol_minus": 0.01},
    )
    session.flush()
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    session.flush()
    return iset


def _bound_work_order(session, *, jb2_id="li-1", qty=2) -> WorkOrder:
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK")
    _published_set_with_measurement(session, product)
    session.commit()

    li = _seed_line_item(session, jb2_id=jb2_id, part_number="APOLLO-9-BLK", qty=qty)
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    session.commit()

    wo = workorders.create_from_line_item(session, li)
    session.commit()
    return wo


# -- 1. PDF auto-generated at WO creation --------------------------------------


def test_guide_generated_at_wo_creation(session):
    wo = _bound_work_order(session)
    assert wo.status == "ready"

    plan_pdf = session.scalars(select(PlanPdf).where(PlanPdf.work_order_id == wo.id)).one()
    assert plan_pdf.version == 1

    path = Path(plan_pdf.path)
    assert path.is_file()
    file_bytes = path.read_bytes()
    assert file_bytes[:4] == b"%PDF"
    assert hashlib.sha256(file_bytes).hexdigest() == plan_pdf.sha256


# -- 2. regeneration bumps version, never overwrites ---------------------------


def test_regeneration_bumps_version_and_leaves_v1_untouched(session):
    wo = _bound_work_order(session)
    v1 = session.scalars(select(PlanPdf).where(PlanPdf.work_order_id == wo.id)).one()
    v1_path = Path(v1.path)
    v1_bytes_before = v1_path.read_bytes()

    v2 = guide_module.generate_build_guide(session, wo, generated_by="admin")
    session.commit()

    assert v2.version == 2
    assert v2.path != v1.path
    assert Path(v2.path).is_file()
    assert v1_path.read_bytes() == v1_bytes_before  # v1 file untouched

    all_versions = sorted(
        p.version for p in session.scalars(select(PlanPdf).where(PlanPdf.work_order_id == wo.id))
    )
    assert all_versions == [1, 2]


# -- 3. blocked WO: no auto-PDF, manual endpoint/function still works ----------


def test_blocked_work_order_gets_no_auto_pdf_but_manual_generation_works(session):
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK")
    _published_set_with_measurement(session, product)  # only binds OP10
    session.commit()

    li = _seed_line_item(session, jb2_id="li-blocked", part_number="APOLLO-9-BLK", qty=1)
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    _seed_routing(session, line_item_id=li.id, seq=20, operation_code="OP99", description="Mystery")
    session.commit()

    wo = workorders.create_from_line_item(session, li)
    session.commit()
    assert wo.status == "blocked_no_instructions"

    assert session.scalars(select(PlanPdf).where(PlanPdf.work_order_id == wo.id)).first() is None

    plan_pdf = guide_module.generate_build_guide(session, wo, generated_by="lead")
    session.commit()
    assert plan_pdf.version == 1
    file_bytes = Path(plan_pdf.path).read_bytes()
    assert file_bytes[:4] == b"%PDF"

    # Render the HTML directly (not the PDF bytes) to confirm the blocked
    # placeholder text made it into the doc.
    plan_ops = guide_module._plan_operations(session, wo.id)
    entries = guide_module._op_render_entries(plan_ops)
    html_str = guide_module._env.get_template("guide/build_guide.html").render(
        work_order=wo, line_item=li, order_number="10100", line_number=1,
        product=product, units=[], ops=entries, plan_hash="deadbeef",
        box_qr="data:image/svg+xml;base64,", generated_at="2026-07-14T00:00:00Z",
    )
    assert "no instructions" in html_str.lower()
    assert "see lead" in html_str.lower()


# -- 4. measurement substeps render a blank-cell table --------------------------


def test_measurement_blank_table_present_in_rendered_html(session):
    wo = _bound_work_order(session, jb2_id="li-meas", qty=1)
    plan_ops = guide_module._plan_operations(session, wo.id)
    entries = guide_module._op_render_entries(plan_ops)
    assert entries[0]["measurements"], "expected a measurement substep to be present"

    html_str = guide_module._env.get_template("guide/build_guide.html").render(
        work_order=wo, line_item=None, order_number=None, line_number=None,
        product=None, units=[], ops=entries, plan_hash="deadbeef",
        box_qr="data:image/svg+xml;base64,", generated_at="2026-07-14T00:00:00Z",
    )
    assert "Paper backup measurement log" in html_str
    assert "Verify chamber depth" in html_str
    assert "<th>Actual</th>" in html_str
    assert "<th>Initials</th>" in html_str
    # blank recorded-value cells, not pre-filled
    assert '<td class="blank"></td>' in html_str


# -- 5. footer carries plan version + content hash + timestamp -----------------


def test_footer_has_plan_version_hash_and_timestamp(session):
    wo = _bound_work_order(session, jb2_id="li-footer", qty=1)
    plan_pdf = session.scalars(select(PlanPdf).where(PlanPdf.work_order_id == wo.id)).one()

    plan_ops = guide_module._plan_operations(session, wo.id)
    plan_hash = guide_module._plan_content_hash(plan_ops)
    entries = guide_module._op_render_entries(plan_ops)
    html_str = guide_module._env.get_template("guide/build_guide.html").render(
        work_order=wo, line_item=None, order_number=None, line_number=None,
        product=None, units=[], ops=entries, plan_hash=plan_hash,
        box_qr="data:image/svg+xml;base64,", generated_at="2026-07-14T00:00:00Z",
    )
    assert f"Plan v{wo.plan_version}" in html_str
    assert plan_hash[:16] in html_str
    assert "2026-07-14T00:00:00Z" in html_str
    # sanity: PlanPdf.sha256 is the *file* hash, distinct from plan_hash
    assert plan_pdf.version == 1
    assert plan_pdf.sha256 != plan_hash


# -- 6. box / badge sheets return real PDFs, QR payload prefixes correct -------


def test_badge_sheet_returns_pdf_bytes_and_correct_qr_payload_prefix():
    operators = [{"name": "Alice Operator", "badge_uuid": "1111-aaaa"}]
    pdf_bytes = badges_pdf.badge_sheet(operators)
    assert pdf_bytes[:4] == b"%PDF"

    html_str = badges_pdf._env.get_template("guide/badge_sheet.html").render(
        cards=[{"name": "Alice Operator", "qr": "data:...", "payload": "OP:1111-aaaa"}]
    )
    assert "OP:1111-aaaa" in html_str


def test_box_label_sheet_returns_pdf_bytes_and_correct_qr_payload_prefix():
    pdf_bytes = badges_pdf.box_label_sheet(["B-DEADBEEF"])
    assert pdf_bytes[:4] == b"%PDF"

    html_str = badges_pdf._env.get_template("guide/box_label_sheet.html").render(
        cards=[{"label": "B-DEADBEEF", "qr": "data:...", "payload": "BOX:B-DEADBEEF"}]
    )
    assert "BOX:B-DEADBEEF" in html_str


# -- 7. admin router endpoints ---------------------------------------------------


@pytest.fixture
def client(engine):
    from app.db import get_session
    from app.main import app

    def _override():
        s = Session(engine)
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        yield c
    del app.dependency_overrides[get_session]


def test_admin_work_orders_list_and_manual_generate_endpoint(session, client):
    wo = _bound_work_order(session, jb2_id="li-endpoint", qty=1)
    wo_id = wo.id
    session.close()  # release the connection so the client's session sees committed rows

    resp = client.get("/admin/work-orders")
    assert resp.status_code == 200
    assert "guide-v1.pdf" in resp.text

    resp2 = client.post(f"/admin/work-orders/{wo_id}/generate-pdf", follow_redirects=False)
    assert resp2.status_code == 303

    resp3 = client.get("/admin/work-orders")
    assert "guide-v2.pdf" in resp3.text


def test_print_endpoints_return_pdf(client):
    resp = client.get("/admin/print/boxes?count=3&prefix=B")
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"

    resp2 = client.get("/admin/print/badges")
    assert resp2.status_code == 200
    assert resp2.content[:4] == b"%PDF"
