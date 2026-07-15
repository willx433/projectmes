"""Identity/auth domain service (P3-03) -- DD §14, state-machine.md §3 (S1/S2)
and §4 (override = second badge).

Stdlib crypto only:
  - PIN hashing: `hashlib.scrypt` (memory-hard KDF, no bcrypt/argon2 dep).
  - Operator-session cookie signing: `hmac` (HMAC-SHA256) -- no itsdangerous.

No new DB tables beyond migration 0007_floor.py (already landed by P3-02).
Operator sessions are NOT a DB table -- see `sign_cookie`/`verify_cookie`
below and app/auth/deps.py's module docstring for why (stateless signed
cookie so idle-expiry works correctly under multiple uvicorn workers).

The one-active-station rule (contract S1: "badge-in elsewhere closes prior
session") is enforced by treating `auth_events` as the source of truth for
"where is this operator's session live right now": `deps.require_operator`
looks up the operator's most recent `kind='login'` row on every request and
rejects the cookie if a newer login exists at a different station. This
needs no extra table -- `auth_events` already exists and is small/indexed
by operator_id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import events
from app.domain.models_floor import OPERATOR_ROLES, AuthEvent, Operator, Station


class AuthError(Exception):
    """Base for all rejected-auth-outcome errors (still logged before raising)."""


class BadgeRejected(AuthError):
    pass


class PinRejected(AuthError):
    pass


class SecondBadgeError(AuthError):
    """P7/state-machine.md §4: second-badge override validation failure."""


def _now() -> datetime:
    # ponytail: explicit microsecond-precision client timestamp rather than
    # relying on AuthEvent.at's server_default=func.now() -- sqlite's
    # CURRENT_TIMESTAMP only resolves to the second, which made two
    # same-second logins (badge-in at station A then B) tie under `ORDER BY
    # at DESC` and break the one-active-station check's "most recent login"
    # lookup. Passing `at=` explicitly fixes ordering on every backend.
    return datetime.now(timezone.utc)


# -- PIN hashing (scrypt, stdlib) -----------------------------------------------

# ponytail: fixed scrypt cost params rather than a tunable-per-install config
# knob -- these are the hashlib docs' recommended interactive-login params
# (n=2**14, r=8, p=1); revisit only if a real perf/security review calls for
# a different cost, not speculatively.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        pin.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, pin_hash: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = pin_hash.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.scrypt(
        pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
    )
    return hmac.compare_digest(candidate, expected)


# -- operator-session cookie signing (HMAC, stdlib) -----------------------------

def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign_cookie(payload: dict, secret: str) -> str:
    """`{body}.{hex hmac-sha256 signature}` -- body is base64url(json)."""
    body = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_cookie(token: str, secret: str) -> dict[str, Any] | None:
    """Returns the decoded payload, or None on any tamper/format failure."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return json.loads(_b64u_decode(body))
    except (ValueError, UnicodeDecodeError):
        return None


# -- operator CRUD ---------------------------------------------------------------

def create_operator(
    session: Session,
    *,
    display_name: str,
    roles: list[str],
    jb2_employee_id: uuid.UUID | None = None,
    pin: str | None = None,
) -> Operator:
    bad_roles = sorted(set(roles) - set(OPERATOR_ROLES))
    if bad_roles:
        raise ValueError(f"unknown role(s): {bad_roles}")
    operator = Operator(
        display_name=display_name,
        jb2_employee_id=jb2_employee_id,
        badge_qr=f"OP:{uuid.uuid4()}",
        pin_hash=hash_pin(pin) if pin else None,
        roles=list(roles),
        active=True,
    )
    session.add(operator)
    session.flush()
    return operator


def revoke_badge(session: Session, operator: Operator) -> Operator:
    """Mint a fresh `OP:{uuid}` badge payload -- the old one no longer
    resolves in `badge_login`. PIN is untouched. Logged to `events` (no
    `auth_events.kind` fits an admin-side revoke, see that table's enum)."""
    old_badge = operator.badge_qr
    operator.badge_qr = f"OP:{uuid.uuid4()}"
    events.emit(
        session,
        "auth.badge_revoked",
        entity=operator,
        actor_id=operator.id,
        before={"badge_qr": old_badge},
        after={"badge_qr": operator.badge_qr},
    )
    session.flush()
    return operator


# -- station enrollment -----------------------------------------------------------

def create_station(
    session: Session,
    *,
    name: str,
    work_center_code: str | None = None,
    location: str | None = None,
) -> tuple[Station, str]:
    """Returns (station, plaintext kiosk token) -- caller displays the token
    exactly once (enrollment UI); it is also what's stored in `stations
    .kiosk_token` (DD §10 models it as plain text, so there is nothing to
    hash against on lookup -- LAN-only device credential, not a login
    secret, per DD §14's network-isolation assumption)."""
    token = secrets.token_urlsafe(32)
    station = Station(
        name=name, work_center_code=work_center_code, location=location, kiosk_token=token,
        active=True,
    )
    session.add(station)
    session.flush()
    return station, token


def resolve_station_by_token(session: Session, token: str) -> Station | None:
    station = session.execute(
        select(Station).where(Station.kiosk_token == token, Station.active.is_(True))
    ).scalar_one_or_none()
    return station


# -- badge / PIN login (contract §3 S1/S2) ---------------------------------------

def _find_operator_by_pin(session: Session, pin: str) -> Operator | None:
    # ponytail: linear scan over active operators with a PIN set, verifying
    # each with scrypt -- correct and simple for floor headcount (tens of
    # operators). Add an indexed lookup (e.g. a PIN-derived lookup key) only
    # if this measurably matters at higher headcount.
    candidates = session.execute(
        select(Operator).where(Operator.active.is_(True), Operator.pin_hash.is_not(None))
    ).scalars()
    for candidate in candidates:
        if verify_pin(pin, candidate.pin_hash):
            return candidate
    return None


def badge_login(
    session: Session,
    station: Station,
    *,
    payload: str | None = None,
    pin: str | None = None,
) -> Operator:
    """Contract S1/S2. Raises `BadgeRejected`/`PinRejected` (both already
    logged to `auth_events` + `events` before raising)."""
    operator: Operator | None = None

    if payload:
        if payload.startswith("OP:"):
            operator = session.execute(
                select(Operator).where(Operator.badge_qr == payload, Operator.active.is_(True))
            ).scalar_one_or_none()
        if operator is None:
            session.add(
                AuthEvent(operator_id=None, station_id=station.id, kind="badge_fail", at=_now())
            )
            events.emit(
                session, "auth.badge_fail", entity=("operator", None), station_id=station.id
            )
            raise BadgeRejected("unknown or revoked badge")
    elif pin:
        operator = _find_operator_by_pin(session, pin)
        if operator is None:
            session.add(
                AuthEvent(operator_id=None, station_id=station.id, kind="pin_fail", at=_now())
            )
            events.emit(
                session, "auth.badge_fail", entity=("operator", None), station_id=station.id
            )
            raise PinRejected("no operator matches that PIN")
    else:
        raise ValueError("badge payload or pin is required")

    # S1: badge-in elsewhere closes the prior session -- log the implicit
    # logout at the old station before opening the new one.
    prior_login = session.execute(
        select(AuthEvent)
        .where(AuthEvent.operator_id == operator.id, AuthEvent.kind == "login")
        .order_by(AuthEvent.at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if prior_login is not None and prior_login.station_id != station.id:
        session.add(
            AuthEvent(
                operator_id=operator.id, station_id=prior_login.station_id,
                kind="logout", at=_now(),
            )
        )
        events.emit(
            session,
            "auth.logout",
            entity=operator,
            actor_id=operator.id,
            station_id=prior_login.station_id,
        )

    session.add(AuthEvent(operator_id=operator.id, station_id=station.id, kind="login", at=_now()))
    events.emit(session, "auth.login", entity=operator, actor_id=operator.id, station_id=station.id)
    session.flush()
    return operator


def logout(session: Session, operator_id: uuid.UUID, station_id: uuid.UUID) -> None:
    session.add(AuthEvent(operator_id=operator_id, station_id=station_id, kind="logout", at=_now()))
    events.emit(
        session, "auth.logout", entity=("operator", operator_id), actor_id=operator_id,
        station_id=station_id,
    )


# -- second-badge override (state-machine.md §4) ----------------------------------

def second_badge(
    session: Session,
    *,
    payload: str,
    role: str,
    actor_operator_id: uuid.UUID | None = None,
) -> Operator:
    """Validates `payload` (an `OP:{uuid}` badge scan from an override
    dialog) belongs to an active operator holding `role`. Does NOT touch the
    caller's own operator-session cookie/state -- it only returns the
    authorizer, or raises `SecondBadgeError`. Every outcome -- accepted or
    rejected -- writes an `auth_events(kind='override')` + `events` row (DD
    §14 "everything privileged records who authorized", including failed
    attempts)."""
    operator = None
    if payload.startswith("OP:"):
        operator = session.execute(
            select(Operator).where(Operator.badge_qr == payload, Operator.active.is_(True))
        ).scalar_one_or_none()

    actor_str = str(actor_operator_id) if actor_operator_id else None

    def _reject(reason: str) -> None:
        session.add(
            AuthEvent(
                operator_id=operator.id if operator else None,
                station_id=None,
                kind="override",
                at=_now(),
            )
        )
        events.emit(
            session,
            "auth.override",
            entity=("operator", operator.id if operator else None),
            actor_id=operator.id if operator else None,
            after={"role": role, "actor_operator_id": actor_str, "rejected": reason},
        )

    if operator is None:
        _reject("unknown or inactive badge")
        raise SecondBadgeError("unknown or inactive badge")
    if role not in (operator.roles or []):
        _reject(f"authorizer lacks role '{role}'")
        raise SecondBadgeError(f"authorizer lacks role '{role}'")
    if actor_operator_id is not None and operator.id == actor_operator_id:
        _reject("authorizer cannot be the acting operator")
        raise SecondBadgeError("authorizer cannot be the acting operator (second badge required)")

    session.add(AuthEvent(operator_id=operator.id, station_id=None, kind="override", at=_now()))
    events.emit(
        session, "auth.override", entity=operator, actor_id=operator.id,
        after={"role": role, "actor_operator_id": actor_str},
    )
    return operator
