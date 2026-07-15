"""POST /scan (P3-04, DD §6.3, state-machine.md §3/§4) -- the one floor
endpoint every kiosk scan (badge or box, hardware-scanned or manually typed
per CR-009) goes through. All validation/mutation lives in
`app.domain.statemachine`; this module only resolves station/operator from
the request and shapes the HTTP response.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import deps, service
from app.config import config
from app.db import get_session
from app.domain import statemachine
from app.domain.models_floor import Operator, Station

router = APIRouter()


class ScanBody(BaseModel):
    payload: str
    request_id: str | None = None
    setup: bool = False
    override_badge: str | None = None


def _optional_operator(
    request: Request, response: Response, station: Station, session: Session
) -> Operator | None:
    """Box scans arrive from an operator who may or may not have an active
    station session (contract S3 handles "none" as a rejection code, not an
    HTTP error) -- reuse `deps.require_operator`'s full check (idle expiry,
    one-active-station rule, cookie refresh) but swallow its 401 into None."""
    try:
        return deps.require_operator(request, response, station, session)
    except HTTPException:
        return None


@router.post("/scan")
def scan(
    body: ScanBody,
    request: Request,
    response: Response,
    station: Station = Depends(deps.require_station),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    operator = _optional_operator(request, response, station, session)

    override_by = None
    if body.override_badge and operator is not None:
        try:
            override_by = deps.second_badge(
                session, payload=body.override_badge, role="lead", actor=operator
            )
        except service.SecondBadgeError as exc:
            session.commit()  # persist the rejected-override auth_events/events row
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    result = statemachine.resolve_scan(
        session, body.payload, station, operator,
        override_by=override_by, setup=body.setup, request_id=body.request_id,
    )
    session.commit()

    if body.payload.startswith("OP:") and result.code == "operator_session_opened":
        now = time.time()
        cookie_payload = {
            "operator_id": result.context["operator_id"],
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

    return result.to_dict()
