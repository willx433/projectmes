"""Identity/auth API + admin UI (P3-03) -- DD §14.

Floor:
  POST /auth/badge    {payload|pin, station_token?} -> sets operator cookie
  POST /auth/logout

Admin (office UI -- see the username/password simplification note below):
  GET/POST /admin/stations   -- enroll a station kiosk (one-time token display)
  GET/POST /admin/operators  -- list/create operators, revoke badge

**Simplification flagged for review (candidate CR, per this task's brief):**
DD §14 says "Office UI uses username/password + role, standard session
auth." There is no `users` table anywhere in the DD §10 schema or migrations
0001-0007, and this task is explicitly told not to invent one. Since
`operators` with role `admin` already has a PIN (the only credential this
schema has for a person), admin/office auth for v1 reuses the existing
badge-or-PIN login (`/auth/badge`) gated by `require_role("admin")` --
there is no separate username/password endpoint. This is NOT a full
implementation of DD §14's office-auth sentence; it is the smallest thing
that satisfies "someone with the admin role must authenticate before
touching /admin/*" without a new table. Escalate to Fable / log a CR if a
real username+password office login is wanted before pilot.
"""
from __future__ import annotations

import time
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import deps, service
from app.config import REPO_ROOT, config
from app.db import get_session
from app.domain.models_floor import OPERATOR_ROLES, Operator, Station

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))


# -- floor: badge/PIN login + logout --------------------------------------------

class BadgeLoginBody(BaseModel):
    payload: str | None = None
    pin: str | None = None
    station_token: str | None = None


def _resolve_station_for_login(
    request: Request, session: Session, body_token: str | None
) -> Station:
    token = (
        body_token
        or request.headers.get("X-Station-Token")
        or request.cookies.get(deps.STATION_COOKIE)
    )
    station = service.resolve_station_by_token(session, token) if token else None
    if station is None:
        raise HTTPException(status_code=401, detail="station token required")
    return station


@router.post("/auth/badge")
def auth_badge(
    body: BadgeLoginBody,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    station = _resolve_station_for_login(request, session, body.station_token)
    try:
        operator = service.badge_login(session, station, payload=body.payload, pin=body.pin)
    except service.AuthError as exc:
        session.commit()  # persist the auth_events/events failure rows
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session.commit()
    now = time.time()
    cookie_payload = {
        "operator_id": str(operator.id),
        "station_id": str(station.id),
        "issued_at": now,
        "last_activity": now,
    }
    response.set_cookie(
        deps.OPERATOR_COOKIE,
        service.sign_cookie(cookie_payload, config.mes_secret_key or ""),
        httponly=True,
        samesite="lax",
    )
    return {
        "operator_id": str(operator.id),
        "display_name": operator.display_name,
        "roles": operator.roles,
        "station_id": str(station.id),
    }


@router.post("/auth/logout")
def auth_logout(
    request: Request, response: Response, session: Session = Depends(get_session)
) -> dict:
    token = request.cookies.get(deps.OPERATOR_COOKIE)
    if token:
        payload = service.verify_cookie(token, config.mes_secret_key or "")
        if payload:
            try:
                operator_id = uuid.UUID(str(payload["operator_id"]))
                station_id = uuid.UUID(str(payload["station_id"]))
            except (KeyError, ValueError):
                operator_id = station_id = None
            if operator_id and station_id:
                service.logout(session, operator_id, station_id)
                session.commit()
    response.delete_cookie(deps.OPERATOR_COOKIE)
    return {"status": "ok"}


# -- admin: station enrollment ---------------------------------------------------

@router.get("/admin/stations")
def admin_stations(request: Request, session: Session = Depends(get_session)):
    stations = list(session.execute(select(Station).order_by(Station.name)).scalars())
    return templates.TemplateResponse(
        request,
        "admin/stations.html",
        {
            "stations": stations,
            "error": request.query_params.get("error"),
            # one-time token display: only present immediately after enroll,
            # passed through the redirect query string.
            "new_token": request.query_params.get("token"),
            "new_station_name": request.query_params.get("name"),
        },
    )


@router.post("/admin/stations")
def admin_create_station(
    name: str = Form(...),
    work_center_code: str = Form(""),
    location: str = Form(""),
    session: Session = Depends(get_session),
):
    station, token = service.create_station(
        session,
        name=name,
        work_center_code=work_center_code or None,
        location=location or None,
    )
    session.commit()
    return RedirectResponse(
        f"/admin/stations?token={quote(token)}&name={quote(name)}", status_code=303
    )


# -- admin: operators (CRUD + badge revoke) -------------------------------------

@router.get("/admin/operators")
def admin_operators(request: Request, session: Session = Depends(get_session)):
    operators = list(session.execute(select(Operator).order_by(Operator.display_name)).scalars())
    return templates.TemplateResponse(
        request,
        "admin/operators.html",
        {
            "operators": operators,
            "all_roles": OPERATOR_ROLES,
            "error": request.query_params.get("error"),
            "new_badge": request.query_params.get("badge"),
        },
    )


@router.post("/admin/operators")
def admin_create_operator(
    display_name: str = Form(...),
    roles: list[str] = Form([]),
    pin: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        operator = service.create_operator(
            session, display_name=display_name, roles=roles, pin=pin or None
        )
    except ValueError as exc:
        session.rollback()
        return RedirectResponse(f"/admin/operators?error={quote(str(exc))}", status_code=303)
    session.commit()
    return RedirectResponse(
        f"/admin/operators?badge={quote(operator.badge_qr)}", status_code=303
    )


@router.post("/admin/operators/{operator_id}/revoke")
def admin_revoke_operator_badge(
    operator_id: uuid.UUID, session: Session = Depends(get_session)
):
    operator = session.get(Operator, operator_id)
    if operator is None:
        return RedirectResponse("/admin/operators?error=operator+not+found", status_code=303)
    service.revoke_badge(session, operator)
    session.commit()
    return RedirectResponse(
        f"/admin/operators?badge={quote(operator.badge_qr)}", status_code=303
    )
