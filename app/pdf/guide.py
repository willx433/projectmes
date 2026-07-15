"""Build Guide PDF generator (P2-11) — DD §8.

`generate_build_guide` renders `templates/guide/build_guide.html` (which
`{% include %}`s the *same* `templates/partials/instruction_steps.html`
partial the station UI/library preview use — DD §8's "single source of
truth" requirement) via a standalone Jinja env, then WeasyPrint turns the
HTML into an immutable, versioned PDF at
`{ARTIFACT_DIR}/{work_order_id}/guide-v{n}.pdf`.

Two distinct hashes are involved, both DD §8 requirements:
  - `plan_hash`: sha256 of the frozen plan JSON (all plan_operations,
    ordered by seq) — printed in the PDF footer as a traceability marker so
    a paper copy can be matched back to the exact frozen plan it came from.
  - `PlanPdf.sha256`: sha256 of the *PDF bytes themselves* — stored on the
    artifact row for integrity checking of the file on disk.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import segno
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.orm import Session
from weasyprint import HTML

from app.config import REPO_ROOT, config
from app.domain.models_execution import PlanOperation, PlanPdf, Unit, WorkOrder
from app.domain.models_jb2 import JB2OrderLineItem
from app.domain.models_library import Product

_env = Environment(
    loader=FileSystemLoader(str(REPO_ROOT / "templates")),
    autoescape=True,
)


def _qr_data_uri(payload: str) -> str:
    """Inline SVG QR code as a data URI — no files on disk (P2-11/P2-12 AC)."""
    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="svg", xmldecl=False, svgns=True)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _plan_operations(session: Session, work_order_id: uuid.UUID) -> list[PlanOperation]:
    return list(
        session.scalars(
            select(PlanOperation)
            .where(PlanOperation.work_order_id == work_order_id)
            .order_by(PlanOperation.seq)
        )
    )


def _plan_content_hash(plan_ops: list[PlanOperation]) -> str:
    payload = [{"seq": op.seq, "frozen_content": op.frozen_content} for op in plan_ops]
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _next_version(session: Session, work_order_id: uuid.UUID) -> int:
    existing = session.scalars(
        select(PlanPdf.version).where(PlanPdf.work_order_id == work_order_id)
    ).all()
    return (max(existing) + 1) if existing else 1


def _op_render_entries(plan_ops: list[PlanOperation]) -> list[dict]:
    """One entry per plan_operation: blocked ops get a placeholder page;
    bound ops get an `iset`/`tree` pair shaped exactly like
    instruction_steps.html expects (DD §5: frozen_content is the source,
    never the live library rows) plus the flat list of measurement substeps
    for the paper-backup blank table."""
    entries = []
    for op in plan_ops:
        entry: dict = {"op": op, "measurements": []}
        if not op.blocked:
            steps = op.frozen_content.get("steps", [])
            entry["iset"] = {
                "title": op.title,
                "version": op.instruction_version,
                "state": "frozen",
                "est_minutes": op.est_minutes,
            }
            entry["tree"] = [{"step": s, "substeps": s.get("substeps", [])} for s in steps]
            for step in steps:
                for sub in step.get("substeps", []):
                    if sub.get("type") == "measurement":
                        entry["measurements"].append(sub)
        entries.append(entry)
    return entries


def generate_build_guide(
    session: Session, work_order: WorkOrder, generated_by: str | None
) -> PlanPdf:
    """Render + write the next immutable version of `work_order`'s build
    guide PDF, recording a `plan_pdfs` row. Works for blocked work orders
    too (manual regeneration per P2-11 brief) — blocked ops just render the
    placeholder page."""
    plan_ops = _plan_operations(session, work_order.id)
    units = list(
        session.scalars(
            select(Unit).where(Unit.work_order_id == work_order.id).order_by(Unit.unit_no)
        )
    )
    line_item = session.get(JB2OrderLineItem, work_order.jb2_line_item_id)
    product = session.get(Product, work_order.product_id)

    plan_hash = _plan_content_hash(plan_ops)
    box_qr = _qr_data_uri(f"WO:{work_order.id}")
    # ponytail: box not assigned until kit-up (Phase 3 build_boxes) — the
    # cover QR encodes the work order itself so a lead can print+bind a real
    # box QR later; this is a placeholder area per the DD §8 cover spec.

    order_number = (line_item.payload or {}).get("orderNumber") if line_item else None
    line_number = (line_item.payload or {}).get("itemNumber") if line_item else None

    html_str = _env.get_template("guide/build_guide.html").render(
        work_order=work_order,
        line_item=line_item,
        order_number=order_number,
        line_number=line_number,
        product=product,
        units=units,
        ops=_op_render_entries(plan_ops),
        plan_hash=plan_hash,
        box_qr=box_qr,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
    )

    version = _next_version(session, work_order.id)
    out_dir = Path(config.artifact_dir or "./artifacts") / str(work_order.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"guide-v{version}.pdf"

    pdf_bytes = HTML(string=html_str, base_url=str(REPO_ROOT / "templates")).write_pdf()
    # immutable: next generation is v+1, this path never gets overwritten again
    out_path.write_bytes(pdf_bytes)

    plan_pdf = PlanPdf(
        id=uuid.uuid4(),
        work_order_id=work_order.id,
        version=version,
        path=str(out_path),
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        generated_by=generated_by,
    )
    session.add(plan_pdf)
    session.flush()
    return plan_pdf
