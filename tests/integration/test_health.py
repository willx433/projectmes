"""Integration tests for the admin health pages (P1-10) — DD §11, §9.9, §17.1.

Zero network: sqlite in-memory DB (app.domain.models_jb2.Base metadata, same
portable-types trick tests/integration/test_outbox.py already uses) + the
fake-JB2 fixture from tests/conftest.py to trip the real circuit breaker.
Verifies all four JSON endpoints, plus /admin/health, stay truthful across
three states: healthy, JB2 down (breaker open), and outbox backlog+parked.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import app.jb2.client as jb2_client_module
from app.db import get_session
from app.domain.models_jb2 import Base, JB2Outbox, SyncRun
from app.jb2.client import Jb2Client, Jb2Error
from app.main import app
from app.sync.worker import REGISTRY
from tests.fake_jb2 import create_fake_jb2, seed_state


@pytest.fixture(autouse=True)
def _reset_jb2_status():
    """The JB2 call status is process-global (app/jb2/client.py's ponytail
    note) -- reset it before each test so breaker state from one test never
    leaks into the next."""
    jb2_client_module._status.update(
        {"breaker_state": "closed", "last_success_at": None, "last_failure_at": None}
    )
    yield


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine):
    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    del app.dependency_overrides[get_session]


def _seed_healthy_sync_runs(session: Session, now: datetime) -> None:
    # Every registered resource needs a recent run or _sync_status marks it
    # stalled -- iterate the real registry so this stays in sync as P1-06/
    # 07/08 grow it, instead of hardcoding a resource-name tuple that goes
    # stale.
    for resource in (rd.name for rd in REGISTRY):
        session.add(
            SyncRun(
                id=uuid.uuid4(),
                resource=resource,
                started_at=now - timedelta(seconds=5),
                finished_at=now,
                fetched=3,
                changed=1,
                error=None,
            )
        )
    session.commit()


def _park_outbox_row(session: Session, created_at: datetime) -> uuid.UUID:
    row_id = uuid.uuid4()
    session.add(
        JB2Outbox(
            id=row_id,
            kind="time_ticket",
            payload={"employeeCode": "E1"},
            idempotency_key=f"wo:{row_id}:op:1:start",
            status="failed",
            attempts=8,
            last_error="JB2 500: server error",
            work_order_id=None,
            created_at=created_at,
        )
    )
    session.commit()
    return row_id


# -- healthy state ------------------------------------------------------------

def test_healthy_state_reflected_truthfully(client, db_session):
    _seed_healthy_sync_runs(db_session, datetime.now(timezone.utc))

    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["jb2_breaker_state"] == "closed"
    assert health["sync_stalled_resources"] == []
    assert health["outbox_failed"] == 0
    assert health["reasons"] == []

    jb2 = client.get("/health/jb2").json()
    assert jb2["breaker_state"] == "closed"

    sync = client.get("/health/sync").json()
    resources = {r["resource"]: r for r in sync["resources"]}
    assert resources["orders"]["stalled"] is False
    assert resources["orders"]["fetched"] == 3

    outbox = client.get("/health/outbox").json()
    assert outbox["counts"] == {"pending": 0, "sent": 0, "confirmed": 0, "failed": 0}
    assert outbox["parked"] == []

    admin = client.get("/admin/health")
    assert admin.status_code == 200
    assert "Sync runs" in admin.text
    assert "circuit breaker is OPEN" not in admin.text


# -- sync stalled --------------------------------------------------------------

def test_stale_sync_run_flagged_stalled(client, db_session):
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.add(
        SyncRun(
            id=uuid.uuid4(), resource="orders", started_at=stale, finished_at=stale,
            fetched=1, changed=1, error=None,
        )
    )
    db_session.commit()

    sync = client.get("/health/sync").json()
    orders = next(r for r in sync["resources"] if r["resource"] == "orders")
    assert orders["stalled"] is True

    health = client.get("/health").json()
    assert health["status"] == "degraded"
    assert any("stalled" in reason for reason in health["reasons"])


# -- JB2 down (breaker open) --------------------------------------------------

def test_jb2_breaker_open_reflected_truthfully(client):
    # Sync bridge onto the fake-JB2 ASGI app, same trick as
    # tests/integration/test_outbox.py (Jb2Client is sync).
    state = seed_state()
    fake_app = create_fake_jb2(state)
    sync_transport = TestClient(fake_app)._transport

    jb2 = Jb2Client(
        "https://api-jb2.example.com",
        "https://auth-jb2.example.com",
        "test-client-id",
        "test-client-secret",
        transport=sync_transport,
        sleeper=lambda s: None,
    )
    try:
        # 5 breaker failures x 3 client-level attempts each (MAX_RETRIES=2).
        state["inject"][("GET", "/api/v1/orders")] = [
            {"status": 500, "body": {}} for _ in range(5 * 3)
        ]
        for _ in range(5):
            try:
                jb2.get("/orders", take=1)
            except Jb2Error:
                pass
        assert jb2.breaker.state == "open"
    finally:
        jb2.close()

    jb2_health = client.get("/health/jb2").json()
    assert jb2_health["breaker_state"] == "open"
    assert jb2_health["last_failure_at"] is not None

    health = client.get("/health").json()
    assert health["status"] == "degraded"
    assert "JB2 circuit breaker open" in health["reasons"]

    admin = client.get("/admin/health")
    assert admin.status_code == 200
    assert "circuit breaker is OPEN" in admin.text


# -- outbox backlog + parked rows, replay ------------------------------------

def test_outbox_backlog_and_parked_row_reflected_and_replay_resets_it(client, db_session):
    old = datetime.now(timezone.utc) - timedelta(seconds=1000)
    row_id = _park_outbox_row(db_session, old)

    outbox = client.get("/health/outbox").json()
    assert outbox["counts"]["failed"] == 1
    assert outbox["oldest_pending_age_s"] > 600
    assert outbox["parked"][0]["id"] == str(row_id)
    assert outbox["parked"][0]["last_error"] == "JB2 500: server error"

    health = client.get("/health").json()
    assert health["status"] == "degraded"
    assert health["outbox_failed"] == 1
    assert any("parked" in r or "backlog" in r for r in health["reasons"])

    admin = client.get("/admin/health")
    assert admin.status_code == 200
    assert "JB2 500: server error" in admin.text
    assert "Replay" in admin.text

    resp = client.post(f"/admin/outbox/{row_id}/replay", follow_redirects=False)
    assert resp.status_code == 303

    db_session.expire_all()
    row = db_session.get(JB2Outbox, row_id)
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.last_error is None

    outbox_after = client.get("/health/outbox").json()
    assert outbox_after["counts"]["failed"] == 0


def test_replay_nonexistent_row_redirects_with_error(client):
    resp = client.post(f"/admin/outbox/{uuid.uuid4()}/replay", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
