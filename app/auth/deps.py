"""FastAPI auth dependencies (P3-03) -- DD §14: every floor action requires
BOTH a station token (device) and an operator session (person).

**Why the operator cookie is stateless, not a DB session table:** the floor
API runs as a single systemd process today (`mes-api`), but the honest
design assumes it may run behind `uvicorn --workers N` later. An in-memory
dict keyed by (station_id, operator_id) would silently stop enforcing idle
expiry correctly the moment a second worker exists (each worker has its own
dict). Putting `last_activity` INSIDE the signed cookie and refreshing it on
every successful request sidesteps that: idle-expiry is computed from data
the client already carries, verified server-side by HMAC (see
app/auth/service.py's sign_cookie/verify_cookie), so it is correct under any
worker count with zero shared state.

The one-active-station rule (contract S1) is the one piece that genuinely
needs shared state across requests/workers -- it's answered by querying
`auth_events` (already a real DB table, already written on every login) for
the operator's most recent login, not by adding a new store.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import service
from app.config import config
from app.db import get_session
from app.domain.models_floor import AuthEvent, Operator, Station

STATION_COOKIE = "mes_station"
OPERATOR_COOKIE = "mes_operator"


def _secret() -> str:
    # ponytail: raise loudly rather than sign/verify cookies with an empty
    # secret -- an unset MES_SECRET_KEY must never silently downgrade auth.
    if not config.mes_secret_key:
        raise HTTPException(status_code=500, detail="MES_SECRET_KEY not configured")
    return config.mes_secret_key


def require_station(
    request: Request,
    session: Session = Depends(get_session),
    x_station_token: str | None = Header(default=None, alias="X-Station-Token"),
) -> Station:
    token = x_station_token or request.cookies.get(STATION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="station token required")
    station = service.resolve_station_by_token(session, token)
    if station is None:
        raise HTTPException(status_code=401, detail="invalid or inactive station token")
    # P4-05 station heartbeat (DD §9.8/N5): every authenticated kiosk request
    # already runs this dependency, so recording it here is the heartbeat --
    # no separate JS ping/endpoint needed. Flushed, not committed: rides
    # along with whatever the route itself commits (a failed request losing
    # one heartbeat tick is harmless, the next request within 15 min renews it).
    station.last_seen_at = datetime.now(timezone.utc)
    session.flush()
    return station


def require_operator(
    request: Request,
    response: Response,
    station: Station = Depends(require_station),
    session: Session = Depends(get_session),
) -> Operator:
    token = request.cookies.get(OPERATOR_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="operator session required")

    payload = service.verify_cookie(token, _secret())
    if payload is None:
        raise HTTPException(status_code=401, detail="invalid or tampered operator session")

    try:
        operator_id = uuid.UUID(str(payload["operator_id"]))
        cookie_station_id = uuid.UUID(str(payload["station_id"]))
        last_activity = float(payload["last_activity"])
        issued_at = float(payload.get("issued_at", last_activity))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="malformed operator session") from exc

    if cookie_station_id != station.id:
        raise HTTPException(
            status_code=401, detail="operator session was issued for a different station"
        )

    idle_limit_s = max(config.station_session_idle_min, 0) * 60
    if time.time() - last_activity > idle_limit_s:
        raise HTTPException(
            status_code=401, detail="operator session idle timeout -- re-badge required"
        )

    operator = session.get(Operator, operator_id)
    if operator is None or not operator.active:
        raise HTTPException(status_code=401, detail="operator no longer active")

    # one-active-station rule (state-machine.md S1): a newer login elsewhere
    # invalidates this cookie even though its own signature/idle window are
    # still fine.
    latest_login = session.execute(
        select(AuthEvent)
        .where(AuthEvent.operator_id == operator.id, AuthEvent.kind == "login")
        .order_by(AuthEvent.at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_login is not None and latest_login.station_id != station.id:
        raise HTTPException(
            status_code=401, detail="operator badged in at another station -- re-badge required"
        )

    new_payload = {
        "operator_id": str(operator.id),
        "station_id": str(station.id),
        "issued_at": issued_at,
        "last_activity": time.time(),
    }
    response.set_cookie(
        OPERATOR_COOKIE,
        service.sign_cookie(new_payload, _secret()),
        httponly=True,
        samesite="lax",
    )
    return operator


def require_role(role: str):
    """Dependency factory: `Depends(require_role("lead"))`."""

    def _dep(operator: Operator = Depends(require_operator)) -> Operator:
        if role not in (operator.roles or []):
            raise HTTPException(status_code=403, detail=f"role '{role}' required")
        return operator

    return _dep


def second_badge(
    session: Session,
    *,
    payload: str,
    role: str,
    actor: Operator,
) -> Operator:
    """Thin wrapper over `service.second_badge` for override dialogs (DD
    §14/state-machine.md §4): validates a second badge scan from the request
    body authorizes `role`, and is not the same operator as `actor` (a
    lead cannot self-authorize their own override). Raises
    `service.SecondBadgeError` (caller maps to HTTP 403/422) -- never
    touches `actor`'s own session cookie."""
    try:
        return service.second_badge(
            session, payload=payload, role=role, actor_operator_id=actor.id
        )
    except service.SecondBadgeError:
        raise


def second_badge_any(
    session: Session, *, payload: str, roles: tuple[str, ...], actor: Operator
) -> Operator:
    """Like `second_badge` but accepts any of several roles (DD §4 O3: "lead
    or quality"). Tries each in turn -- every attempt is audited by
    `service.second_badge` itself, so a multi-role check may log more than
    one auth_events row; acceptable, it's still an accurate record of what
    was tried. Shared by app/api/substeps.py's per-substep disposition
    dialog and app/api/station.py's whole-operation Fail screen (P3-R2) --
    was a private helper duplicated in substeps.py; moved here so both
    routers route through one implementation."""
    last_exc: service.SecondBadgeError | None = None
    for role in roles:
        try:
            return second_badge(session, payload=payload, role=role, actor=actor)
        except service.SecondBadgeError as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc
