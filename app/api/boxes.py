"""Kit-up + box lifecycle admin UI (P3-11, DD §6.1.5/§17.6).

This is an office/admin page, same authless convention as the rest of
`/admin/*` today (see app/api/auth.py's module docstring -- there's no
`users`/office-session table yet, so admin routes aren't uniformly
role-gated). The one piece of identity this flow genuinely needs -- "who is
doing the kit-up / reassignment" -- is captured as a badge payload typed or
scanned into the form (same `OP:{uuid}` payload the floor uses), resolved
directly against `operators` rather than through the cookie-based station
session (there's no station token on an office page).
"""
from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT, config
from app.db import get_session
from app.domain import boxes
from app.domain.models_execution import Unit, WorkOrder
from app.domain.models_floor import BuildBox, Operator, Station

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))


def _operator_by_badge(session: Session, payload: str) -> Operator:
    payload = (payload or "").strip()
    operator = None
    if payload.startswith("OP:"):
        operator = session.execute(
            select(Operator).where(Operator.badge_qr == payload, Operator.active.is_(True))
        ).scalar_one_or_none()
    if operator is None:
        raise boxes.BoxError("unknown or inactive badge")
    return operator


def _require_lead(session: Session, payload: str) -> Operator:
    operator = _operator_by_badge(session, payload)
    if "lead" not in (operator.roles or []):
        raise boxes.BoxError("a lead badge is required for this action")
    return operator


# -- kit-up ----------------------------------------------------------------------


@router.get("/admin/kitup")
def admin_kitup(request: Request, session: Session = Depends(get_session)):
    work_orders = boxes.work_orders_needing_kitup(session)

    selected_id = request.query_params.get("work_order_id")
    selected_wo = None
    units = []
    if selected_id:
        try:
            selected_wo = session.get(WorkOrder, uuid.UUID(selected_id))
        except ValueError:
            selected_wo = None
        if selected_wo is not None:
            units = boxes.boxless_units(session, selected_wo.id)

    return templates.TemplateResponse(
        request,
        "admin/kitup.html",
        {
            "work_orders": work_orders,
            "selected_wo": selected_wo,
            "units": units,
            "kitup_requires_lead": config.kitup_requires_lead,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


@router.post("/admin/kitup")
def admin_do_kitup(
    work_order_id: uuid.UUID = Form(...),
    unit_id: uuid.UUID = Form(...),
    box_qr: str = Form(...),
    serial: str = Form(""),
    operator_badge: str = Form(...),
    session: Session = Depends(get_session),
):
    def _err(message: str) -> RedirectResponse:
        session.rollback()
        return RedirectResponse(
            f"/admin/kitup?work_order_id={work_order_id}&error={quote(message)}",
            status_code=303,
        )

    unit = session.get(Unit, unit_id)
    if unit is None or unit.work_order_id != work_order_id:
        return _err("unit not found on this work order")

    try:
        operator = _operator_by_badge(session, operator_badge)
        if config.kitup_requires_lead and "lead" not in (operator.roles or []):
            raise boxes.BoxError("kit-up requires a lead badge (KITUP_REQUIRES_LEAD)")
        box = boxes.register_box(session, box_qr.strip())
        boxes.assign_box(session, box, unit, operator, serial.strip() or None)
    except boxes.BoxError as exc:
        return _err(str(exc))

    session.commit()
    return RedirectResponse(
        f"/admin/kitup?work_order_id={work_order_id}&success={quote('box assigned')}",
        status_code=303,
    )


# -- box list + reassignment ------------------------------------------------------


@router.get("/admin/boxes")
def admin_boxes(request: Request, session: Session = Depends(get_session)):
    box_rows = list(session.execute(select(BuildBox).order_by(BuildBox.qr_payload)).scalars())
    unit_ids = {b.current_unit_id for b in box_rows if b.current_unit_id is not None}
    units_by_id = {
        u.id: u for u in session.execute(select(Unit).where(Unit.id.in_(unit_ids))).scalars()
    } if unit_ids else {}
    station_ids = {b.current_station_id for b in box_rows if b.current_station_id is not None}
    stations_by_id = {
        s.id: s
        for s in session.execute(select(Station).where(Station.id.in_(station_ids))).scalars()
    } if station_ids else {}

    rows = [
        {
            "box": b,
            "unit": units_by_id.get(b.current_unit_id),
            "station": stations_by_id.get(b.current_station_id),
        }
        for b in box_rows
    ]
    return templates.TemplateResponse(
        request,
        "admin/boxes.html",
        {"rows": rows, "error": request.query_params.get("error"),
         "success": request.query_params.get("success")},
    )


@router.post("/admin/boxes/{box_id}/reassign")
def admin_reassign_box(
    box_id: uuid.UUID,
    new_box_qr: str = Form(...),
    lead_badge: str = Form(...),
    retire_old: bool = Form(False),
    session: Session = Depends(get_session),
):
    def _err(message: str) -> RedirectResponse:
        session.rollback()
        return RedirectResponse(f"/admin/boxes?error={quote(message)}", status_code=303)

    old_box = session.get(BuildBox, box_id)
    if old_box is None:
        return _err("box not found")
    if old_box.current_unit_id is None:
        return _err("box has no unit assigned to reassign")
    unit = session.get(Unit, old_box.current_unit_id)

    try:
        lead = _require_lead(session, lead_badge)
        new_box = boxes.register_box(session, new_box_qr.strip())
        boxes.reassign_box(session, old_box, new_box, unit, lead, retire_old=retire_old)
    except boxes.BoxError as exc:
        return _err(str(exc))

    session.commit()
    return RedirectResponse(f"/admin/boxes?success={quote('box reassigned')}", status_code=303)
