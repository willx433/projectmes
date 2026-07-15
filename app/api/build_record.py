"""Per-serial build-record PDF endpoints (P4-08) -- DD §18.7/§9.

Mirrors app/api/workorders.py's generate-pdf + traversal-safe-serve pair,
but keyed on a completed Unit instead of a WorkOrder, and lazy: nothing
auto-generates this when a unit reaches `done` (see app/pdf/build_record.py
module docstring for why) -- it's produced the first time this page's
button is clicked. `/admin/units` is a new, minimal listing page (a
concurrent P4-03 agent's `app/api/unit_detail.py` -- out of scope for this
task to edit -- owns the richer per-unit `/units/{id}` timeline; this
page's serial links there and otherwise just needs a generate/download
button, so it doesn't duplicate that work).
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT, config
from app.db import get_session
from app.domain.models_execution import Unit, WorkOrder
from app.domain.models_library import Product
from app.pdf.build_record import generate_build_record, record_path, unit_key

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))


def _records_root() -> Path:
    return Path(config.artifact_dir or "./artifacts") / "build-records"


@router.get("/admin/units")
def admin_units(request: Request, session: Session = Depends(get_session)):
    units = list(
        session.scalars(
            select(Unit).where(Unit.status == "done").order_by(Unit.completed_at.desc())
        )
    )
    work_orders = {wo.id: wo for wo in session.scalars(select(WorkOrder))}
    products = {p.id: p for p in session.scalars(select(Product))}

    rows = []
    for unit in units:
        wo = work_orders.get(unit.work_order_id)
        product = products.get(wo.product_id) if wo else None
        rows.append(
            {
                "unit": unit,
                "work_order": wo,
                "product": product,
                "record_url": f"/artifacts/build-records/{unit_key(unit)}/record-v1.pdf",
                "record_exists": record_path(unit).is_file(),
            }
        )
    return templates.TemplateResponse(
        request,
        "admin/units.html",
        {"rows": rows, "error": request.query_params.get("error")},
    )


@router.post("/admin/units/{unit_id}/build-record")
def admin_generate_build_record(unit_id: uuid.UUID, session: Session = Depends(get_session)):
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unit not found")
    if unit.status != "done":
        raise HTTPException(
            status_code=409, detail="build record is only available once the unit is done"
        )
    generate_build_record(session, unit, generated_by="admin")
    # nothing mutated on `session` -- generate_build_record only writes a
    # file (see its module docstring: no persisted row for this artifact).
    return RedirectResponse("/admin/units", status_code=303)


@router.get("/artifacts/build-records/{key}/{filename}")
def download_build_record(key: str, filename: str) -> FileResponse:
    records_root = _records_root().resolve()
    base = (records_root / key).resolve()
    path = (base / filename).resolve()
    # path-traversal guard, two checks (not just the one media.py/
    # workorders.py need): `base` must resolve to a direct child of
    # records_root -- blocks `key=".."` escaping build-records/ entirely --
    # and `path` must resolve to a direct child of `base` -- blocks
    # `filename` traversal, the part the single-check version of this
    # guard elsewhere in the codebase also does. The extra check exists
    # here specifically because `key` is free text (a unit's
    # operator-entered serial number, see app.pdf.build_record.unit_key),
    # unlike e.g. workorders.py's `work_order_id`, which FastAPI already
    # constrains to a real `uuid.UUID` before this code ever runs.
    if base.parent != records_root or path.parent != base or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/pdf")
