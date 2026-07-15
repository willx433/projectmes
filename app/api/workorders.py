"""Work order visibility + PDF generation/printing (P2-11/P2-12/P2-13 surface)
— DD §8 (build guide PDF), §14 (badge/box QR printing).

`GET /admin/work-orders` is the minimal admin list this task calls for; it
doubles as P2-13's status-flag visibility surface (blocked_no_instructions /
routing_drift / cancel_requested all show up as badges here already, since
they're just `WorkOrder.status` values).
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT, config
from app.db import get_session
from app.domain.models_execution import PlanPdf, WorkOrder
from app.domain.models_jb2 import JB2Employee
from app.domain.models_library import Product
from app.pdf import badges as badges_pdf
from app.pdf.guide import generate_build_guide

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))


def _artifact_root() -> Path:
    return Path(config.artifact_dir or "./artifacts")


# -- work order list + PDF generation ------------------------------------------

@router.get("/admin/work-orders")
def admin_work_orders(request: Request, session: Session = Depends(get_session)):
    work_orders = list(
        session.scalars(select(WorkOrder).order_by(WorkOrder.created_at.desc()))
    )
    products = {p.id: p for p in session.scalars(select(Product))}
    pdfs_by_wo: dict[uuid.UUID, list[PlanPdf]] = {}
    for pdf in session.scalars(select(PlanPdf).order_by(PlanPdf.version.desc())):
        pdfs_by_wo.setdefault(pdf.work_order_id, []).append(pdf)

    rows = [
        {
            "wo": wo,
            "product": products.get(wo.product_id),
            "pdfs": pdfs_by_wo.get(wo.id, []),
        }
        for wo in work_orders
    ]
    return templates.TemplateResponse(
        request,
        "admin/work_orders.html",
        {"rows": rows, "error": request.query_params.get("error")},
    )


@router.post("/admin/work-orders/{work_order_id}/generate-pdf")
def admin_generate_pdf(work_order_id: uuid.UUID, session: Session = Depends(get_session)):
    work_order = session.get(WorkOrder, work_order_id)
    if work_order is None:
        raise HTTPException(status_code=404, detail="work order not found")
    generate_build_guide(session, work_order, generated_by="admin")
    session.commit()
    return RedirectResponse("/admin/work-orders", status_code=303)


@router.get("/artifacts/work-orders/{work_order_id}/{filename}")
def download_work_order_pdf(work_order_id: uuid.UUID, filename: str) -> FileResponse:
    base = (_artifact_root() / str(work_order_id)).resolve()
    path = (base / filename).resolve()
    # path-traversal guard: resolved path must still live directly under base
    if path.parent != base or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/pdf")


# -- badge / box label printing (P2-12, DD §14) --------------------------------

@router.get("/admin/print/badges")
def print_badges(session: Session = Depends(get_session)) -> Response:
    """Badge sheet for every jb2_employees mirror row. ponytail: badge uuid
    is minted fresh on every print -- there's no `operators` table yet
    (Phase 3), so nothing persists a stable badge<->employee uuid across
    reprints. Upgrade when Phase 3 lands: store the uuid on the operator
    row instead of generating one here."""
    employees = session.scalars(
        select(JB2Employee).where(JB2Employee.active.is_(True)).order_by(JB2Employee.name)
    ).all()
    operators = [
        {"name": e.name or e.employee_code or str(e.id), "badge_uuid": str(uuid.uuid4())}
        for e in employees
    ]
    pdf_bytes = badges_pdf.badge_sheet(operators)
    return Response(content=pdf_bytes, media_type="application/pdf")


@router.get("/admin/print/boxes")
def print_boxes(count: int = 10, prefix: str = "B") -> Response:
    """Fresh box labels: `{prefix}-{uuid suffix}` per label, QR BOX:{label}."""
    if count < 1 or count > 200:
        raise HTTPException(status_code=400, detail="count must be between 1 and 200")
    labels = [f"{prefix}-{uuid.uuid4().hex[:8].upper()}" for _ in range(count)]
    pdf_bytes = badges_pdf.box_label_sheet(labels)
    return Response(content=pdf_bytes, media_type="application/pdf")
