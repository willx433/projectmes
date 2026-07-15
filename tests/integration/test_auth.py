"""Integration tests for app/auth (P3-03) -- DD §14, state-machine.md §3
(S1/S2 scan resolution) and §4 (override = second badge).

Zero-network sqlite, same pattern as tests/integration/test_media.py: a
fresh engine per test, `app.domain.models_jb2.Base.metadata.create_all`
(picks up operators/stations/auth_events transitively since
app.domain.models_floor shares that Base), and `dataclasses.replace` to
swap in a test `MES_SECRET_KEY`/idle window without touching the real
`app.config.config` singleton.

Rather than reuse the shared `app.main.app` singleton (which has no
protected floor routes yet -- statemachine.py/P3-04 is a separate task),
tests build a minimal standalone FastAPI app: the real `app.api.auth`
router plus one tiny `/  _test/protected` route guarded by
`deps.require_operator` / `deps.require_role("lead")`, so the dependencies
themselves are exercised exactly as a future floor endpoint would use them.
"""
from __future__ import annotations

import dataclasses
import time

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.auth import deps, service
from app.config import config as real_config
from app.db import get_session
from app.domain.models_floor import AuthEvent, Event, Operator
from app.domain.models_jb2 import Base

# Registers the full model graph (work_orders etc.) on Base.metadata so
# jb2_outbox's FK to work_orders resolves under Base.metadata.create_all.
from app.main import app as _main_app  # noqa: F401

TEST_SECRET = "unit-test-secret-key-not-for-prod"


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    # ponytail: same sqlite/bigint-autoincrement workaround as
    # tests/unit/test_events.py -- Event.id is BigInteger for Postgres
    # bigserial parity, but sqlite only rowid-aliases a PK declared as
    # exactly INTEGER. Swap the type for CREATE TABLE only, restore after.
    original_type = Event.__table__.c.id.type
    Event.__table__.c.id.type = Integer()
    try:
        Base.metadata.create_all(eng)
    finally:
        Event.__table__.c.id.type = original_type
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


def _build_app(idle_min: int = 10) -> FastAPI:
    from app.api.auth import router as auth_router

    test_config = dataclasses.replace(
        real_config, mes_secret_key=TEST_SECRET, station_session_idle_min=idle_min
    )

    app = FastAPI()
    app.include_router(auth_router)

    @app.get("/_test/protected")
    def _protected(operator: Operator = Depends(deps.require_operator)) -> dict:
        return {"operator_id": str(operator.id)}

    @app.get("/_test/lead-only")
    def _lead_only(operator: Operator = Depends(deps.require_role("lead"))) -> dict:
        return {"operator_id": str(operator.id)}

    app.state._test_config = test_config  # keep alive / discoverable
    return app, test_config


@pytest.fixture
def app_and_config():
    return _build_app()


@pytest.fixture
def client(engine, app_and_config, monkeypatch):
    app, test_config = app_and_config
    monkeypatch.setattr("app.api.auth.config", test_config)
    monkeypatch.setattr("app.auth.deps.config", test_config)

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


def _make_station(db_session, name="Station 1"):
    station, token = service.create_station(db_session, name=name)
    db_session.commit()
    return station, token


def _make_operator(db_session, name="Alice", roles=None, pin=None):
    operator = service.create_operator(
        db_session, display_name=name, roles=roles or ["operator"], pin=pin
    )
    db_session.commit()
    return operator


# -- badge login happy path / unknown badge -------------------------------------

def test_badge_login_happy_path(client, db_session):
    station, token = _make_station(db_session)
    operator = _make_operator(db_session, roles=["operator"])

    resp = client.post(
        "/auth/badge",
        json={"payload": operator.badge_qr},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["operator_id"] == str(operator.id)
    assert "mes_operator" in resp.cookies

    events = db_session.execute(
        select(AuthEvent).where(AuthEvent.operator_id == operator.id)
    ).scalars().all()
    assert any(e.kind == "login" and e.station_id == station.id for e in events)


def test_badge_login_unknown_badge(client, db_session):
    _, token = _make_station(db_session)

    resp = client.post(
        "/auth/badge",
        json={"payload": "OP:00000000-0000-0000-0000-000000000000"},
        headers={"X-Station-Token": token},
    )
    assert resp.status_code == 401

    fails = db_session.execute(
        select(AuthEvent).where(AuthEvent.kind == "badge_fail")
    ).scalars().all()
    assert len(fails) == 1


# -- PIN fallback ----------------------------------------------------------------

def test_pin_login_fallback(client, db_session):
    _, token = _make_station(db_session)
    operator = _make_operator(db_session, name="Bob", pin="1234")

    resp = client.post(
        "/auth/badge", json={"pin": "1234"}, headers={"X-Station-Token": token}
    )
    assert resp.status_code == 200
    assert resp.json()["operator_id"] == str(operator.id)


def test_pin_login_wrong_pin_rejected(client, db_session):
    _, token = _make_station(db_session)
    _make_operator(db_session, name="Bob", pin="1234")

    resp = client.post(
        "/auth/badge", json={"pin": "9999"}, headers={"X-Station-Token": token}
    )
    assert resp.status_code == 401
    fails = db_session.execute(
        select(AuthEvent).where(AuthEvent.kind == "pin_fail")
    ).scalars().all()
    assert len(fails) == 1


# -- one-active-station rule (S1) -------------------------------------------------

def test_one_station_rule_closes_prior_session(client, db_session):
    station_a, token_a = _make_station(db_session, name="A")
    station_b, token_b = _make_station(db_session, name="B")
    operator = _make_operator(db_session)

    resp_a = client.post(
        "/auth/badge", json={"payload": operator.badge_qr}, headers={"X-Station-Token": token_a}
    )
    assert resp_a.status_code == 200
    cookie_a = resp_a.cookies["mes_operator"]

    # badge in at station B -- should close the session at A per contract S1
    resp_b = client.post(
        "/auth/badge", json={"payload": operator.badge_qr}, headers={"X-Station-Token": token_b}
    )
    assert resp_b.status_code == 200

    logouts = db_session.execute(
        select(AuthEvent).where(
            AuthEvent.operator_id == operator.id, AuthEvent.kind == "logout"
        )
    ).scalars().all()
    assert any(e.station_id == station_a.id for e in logouts)

    # the OLD cookie (station A) is now stale -- using it against station A
    # must be rejected even though its idle window and signature are fine.
    client.cookies.set("mes_operator", cookie_a)
    resp = client.get("/_test/protected", headers={"X-Station-Token": token_a})
    assert resp.status_code == 401


# -- idle expiry -------------------------------------------------------------------

def test_idle_expiry(engine, monkeypatch):
    app, test_config = _build_app(idle_min=10)
    monkeypatch.setattr("app.api.auth.config", test_config)
    monkeypatch.setattr("app.auth.deps.config", test_config)

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override

    with Session(engine) as db_session:
        station, token = _make_station(db_session)
        operator = _make_operator(db_session)
        station_id, operator_id, badge_qr = station.id, operator.id, operator.badge_qr

    with TestClient(app) as client:
        resp = client.post(
            "/auth/badge", json={"payload": badge_qr}, headers={"X-Station-Token": token}
        )
        assert resp.status_code == 200

        # fresh cookie works
        ok = client.get("/_test/protected", headers={"X-Station-Token": token})
        assert ok.status_code == 200

        # craft a stale cookie: last_activity 11 minutes in the past (> 10 min idle limit)
        stale_payload = {
            "operator_id": str(operator_id),
            "station_id": str(station_id),
            "issued_at": time.time() - 700,
            "last_activity": time.time() - 700,
        }
        stale_cookie = service.sign_cookie(stale_payload, TEST_SECRET)
        client.cookies.set("mes_operator", stale_cookie)
        resp = client.get("/_test/protected", headers={"X-Station-Token": token})
        assert resp.status_code == 401


# -- require_role ------------------------------------------------------------------

def test_require_role_rejects_missing_role(client, db_session):
    _, token = _make_station(db_session)
    operator = _make_operator(db_session, roles=["operator"])  # no "lead"

    client.post(
        "/auth/badge", json={"payload": operator.badge_qr}, headers={"X-Station-Token": token}
    )
    resp = client.get("/_test/lead-only", headers={"X-Station-Token": token})
    assert resp.status_code == 403


def test_require_role_allows_matching_role(client, db_session):
    _, token = _make_station(db_session)
    operator = _make_operator(db_session, roles=["operator", "lead"])

    client.post(
        "/auth/badge", json={"payload": operator.badge_qr}, headers={"X-Station-Token": token}
    )
    resp = client.get("/_test/lead-only", headers={"X-Station-Token": token})
    assert resp.status_code == 200


# -- second badge (override matrix, state-machine.md §4) --------------------------

def test_second_badge_validates_role(db_session):
    lead = _make_operator(db_session, name="Lead", roles=["lead"])
    operator = _make_operator(db_session, name="Operator", roles=["operator"])

    # wrong role
    with pytest.raises(service.SecondBadgeError):
        deps.second_badge(db_session, payload=operator.badge_qr, role="lead", actor=operator)

    # correct role, different actor -> ok
    authorizer = deps.second_badge(
        db_session, payload=lead.badge_qr, role="lead", actor=operator
    )
    assert authorizer.id == lead.id


def test_second_badge_rejects_self_authorization(db_session):
    lead = _make_operator(db_session, name="Lead", roles=["lead"])

    with pytest.raises(service.SecondBadgeError):
        deps.second_badge(db_session, payload=lead.badge_qr, role="lead", actor=lead)


# -- station token required for floor deps -----------------------------------------

def test_station_token_required(client):
    resp = client.get("/_test/protected")
    assert resp.status_code == 401


def test_unknown_station_token_rejected(client):
    resp = client.get("/_test/protected", headers={"X-Station-Token": "bogus"})
    assert resp.status_code == 401


# -- cookie tamper -------------------------------------------------------------------

def test_tampered_cookie_rejected(client, db_session):
    _, token = _make_station(db_session)
    operator = _make_operator(db_session)

    resp = client.post(
        "/auth/badge", json={"payload": operator.badge_qr}, headers={"X-Station-Token": token}
    )
    cookie = resp.cookies["mes_operator"]
    body, sig = cookie.rsplit(".", 1)
    tampered = f"{body}.{'0' * len(sig)}"
    client.cookies.set("mes_operator", tampered)

    resp = client.get("/_test/protected", headers={"X-Station-Token": token})
    assert resp.status_code == 401


# -- auth_events rows for each outcome ----------------------------------------------

def test_auth_events_recorded_for_login_and_logout(client, db_session):
    _, token = _make_station(db_session)
    operator = _make_operator(db_session)

    client.post(
        "/auth/badge", json={"payload": operator.badge_qr}, headers={"X-Station-Token": token}
    )
    client.post("/auth/logout")

    kinds = [
        e.kind
        for e in db_session.execute(
            select(AuthEvent).where(AuthEvent.operator_id == operator.id)
        ).scalars()
    ]
    assert "login" in kinds
    assert "logout" in kinds
