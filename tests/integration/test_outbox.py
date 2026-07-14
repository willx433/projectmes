"""Integration tests for app/outbox (P1-09) — zero network.

Fake-JB2 is bridged to the sync Jb2Client via starlette's TestClient
transport (the same ASGI app tests/fake_jb2 already uses, wrapped so a
sync httpx.Client can drive it — Jb2Client is sync, ASGITransport is
async-only). DB is sqlite in-memory: app/domain/models_jb2.py already
defines a portable JSON/Uuid type pair for exactly this (see its
`_JSONB`/`Uuid` ponytail note), so no extra type shims are needed here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.domain.models_jb2 import JB2Outbox
from app.jb2.client import Jb2Client
from app.outbox import drainer, writer
from tests.fake_jb2 import create_fake_jb2, seed_state


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    JB2Outbox.__table__.create(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def state():
    return seed_state()


@pytest.fixture
def jb2_client(state):
    app = create_fake_jb2(state)
    transport = TestClient(app)._transport  # sync bridge onto the ASGI app
    client = Jb2Client(
        "https://api-jb2.example.com",
        "https://auth-jb2.example.com",
        "test-client-id",
        "test-client-secret",
        transport=transport,
        sleeper=lambda s: None,
    )
    yield client
    client.close()


def _payload(**kw):
    return {"employeeCode": "E1", "workCenter": "WC1", **kw}


# -- same-transaction guarantee -------------------------------------------

def test_enqueue_rolled_back_with_caller_transaction(session):
    writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    session.rollback()
    assert session.query(JB2Outbox).count() == 0


def test_enqueue_committed_with_caller_transaction(session):
    writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    session.commit()
    assert session.query(JB2Outbox).count() == 1


# -- idempotent enqueue -----------------------------------------------------

def test_duplicate_enqueue_is_idempotent(session):
    id1 = writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    id2 = writer.enqueue(
        session, "time_ticket", _payload(note="different body"), "wo:1:op:10:start"
    )
    session.commit()
    assert id1 == id2
    assert session.query(JB2Outbox).count() == 1


def test_make_key_format():
    assert writer.make_key("wo", "abc", "op", 10, "finish") == "wo:abc:op:10:finish"


# -- drain posts and is idempotent at the drain level -----------------------

def test_drain_posts_to_fake_jb2_and_never_double_posts(session, jb2_client, state):
    writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    session.commit()

    outcomes1 = drainer.drain_once(session, jb2_client)
    assert outcomes1 == [(_only_row(session).id, "confirmed")]
    assert len(state["received_writes"]) == 1

    outcomes2 = drainer.drain_once(session, jb2_client)
    assert outcomes2 == []  # already confirmed, not re-fetched
    assert len(state["received_writes"]) == 1

    row = _only_row(session)
    assert row.status == "confirmed"
    assert row.sent_at is not None
    assert row.confirmed_at is not None


def test_time_ticket_detail_kind_posts_to_detail_endpoint(session, jb2_client, state):
    writer.enqueue(session, "time_ticket_detail", _payload(piecesFinished=5), "wo:1:op:10:detail")
    session.commit()

    drainer.drain_once(session, jb2_client)

    assert state["received_writes"][0]["path"] == "/time-ticket-details"


# -- per-work-order FIFO -----------------------------------------------------

def test_per_work_order_fifo_blocks_on_failure_but_not_other_streams(session, jb2_client, state):
    wo_a = uuid.uuid4()
    wo_b = uuid.uuid4()

    r1 = writer.enqueue(session, "time_ticket", _payload(seq=1), "wo:a:op:10:start", wo_a)
    r2 = writer.enqueue(session, "time_ticket", _payload(seq=2), "wo:a:op:20:start", wo_a)
    r3 = writer.enqueue(session, "time_ticket", _payload(seq=3), "wo:a:op:30:start", wo_a)
    r_other = writer.enqueue(session, "time_ticket", _payload(seq=4), "wo:b:op:10:start", wo_b)
    session.commit()

    # Make the middle row (r2) permanently reject with a 4xx. wo_a's stream
    # is processed before wo_b's (created earlier), so this queue is
    # consumed by r1 then r2 only — r3 is never attempted, and r_other
    # (a different path prefix match, but same endpoint) falls through to
    # the fake server's normal 201 once the queue is empty.
    state["inject"][("POST", "/api/v1/time-tickets")] = [
        {"status": 201, "body": {}},  # r1 succeeds
        {"status": 400, "body": {"Title": "Bad Request"}},  # r2 parks
    ]

    drainer.drain_once(session, jb2_client)

    def status_of(row_id):
        return session.get(JB2Outbox, row_id).status

    assert status_of(r1) == "confirmed"
    assert status_of(r2) == "failed"
    assert status_of(r3) == "pending"  # blocked behind r2 — never attempted
    assert status_of(r_other) == "confirmed"  # unrelated WO drains independently

    # r1 and r2 consumed both injected responses; r_other used the normal 201.
    assert state["inject"][("POST", "/api/v1/time-tickets")] == []


# -- retry with backoff then success -----------------------------------------

def test_500_retries_with_backoff_then_succeeds(session, jb2_client, state):
    writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    session.commit()

    # Jb2Client itself retries 5xx up to MAX_RETRIES (2) before giving up —
    # 3 total client-level attempts — so exhausting *that* budget is what
    # makes one outbox-level drain attempt surface as a failure needing the
    # drainer's own backoff.
    state["inject"][("POST", "/api/v1/time-tickets")] = [
        {"status": 500, "body": {"Title": "Server Error"}} for _ in range(3)
    ]

    now0 = datetime.now(timezone.utc)
    outcomes = drainer.drain_once(session, jb2_client, now=now0)
    assert outcomes[0][1] == "retry"
    row = _only_row(session)
    assert row.status == "pending"
    assert row.attempts == 1
    next_attempt_at = drainer._aware(row.next_attempt_at)
    assert next_attempt_at > now0
    assert row.last_error

    # Not due yet — draining again immediately does nothing.
    assert drainer.drain_once(session, jb2_client, now=now0) == []
    assert len(state["received_writes"]) == 0

    # Once due, the (now-clear) endpoint accepts it.
    later = next_attempt_at + timedelta(seconds=1)
    outcomes = drainer.drain_once(session, jb2_client, now=later)
    assert outcomes[0][1] == "confirmed"
    assert len(state["received_writes"]) == 1


def test_max_attempts_parks_as_failed(session, jb2_client, state):
    writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    session.commit()

    # 3 client-level attempts (its own retry budget) per outbox-level attempt.
    state["inject"][("POST", "/api/v1/time-tickets")] = [
        {"status": 500, "body": {}} for _ in range(3 * drainer.MAX_ATTEMPTS)
    ]

    now = datetime.now(timezone.utc)
    for _ in range(drainer.MAX_ATTEMPTS):
        drainer.drain_once(session, jb2_client, now=now)
        row = _only_row(session)
        if row.status == "failed":
            break
        now = drainer._aware(row.next_attempt_at) + timedelta(seconds=1)

    row = _only_row(session)
    assert row.status == "failed"
    assert row.attempts == drainer.MAX_ATTEMPTS
    assert row.last_error


# -- permanent 4xx parks immediately, no retries ------------------------------

def test_400_parks_immediately_with_last_error(session, jb2_client, state):
    writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    session.commit()

    state["inject"][("POST", "/api/v1/time-tickets")] = [
        {"status": 400, "body": {"Title": "Bad Request", "Detail": "nope"}},
    ]

    outcomes = drainer.drain_once(session, jb2_client)
    assert outcomes[0][1] == "failed"

    row = _only_row(session)
    assert row.status == "failed"
    assert row.attempts == 1  # never retried
    assert "400" in row.last_error


# -- manual replay ------------------------------------------------------------

def test_replay_resets_failed_row_and_it_redrains(session, jb2_client, state):
    writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    session.commit()

    state["inject"][("POST", "/api/v1/time-tickets")] = [
        {"status": 400, "body": {}},
    ]
    drainer.drain_once(session, jb2_client)
    row = _only_row(session)
    assert row.status == "failed"

    drainer.replay(session, row.id)
    row = _only_row(session)
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.last_error is None

    drainer.drain_once(session, jb2_client)
    row = _only_row(session)
    assert row.status == "confirmed"
    assert len(state["received_writes"]) == 1


def test_replay_rejects_non_failed_row(session):
    outbox_id = writer.enqueue(session, "time_ticket", _payload(), "wo:1:op:10:start")
    session.commit()
    with pytest.raises(ValueError):
        drainer.replay(session, outbox_id)


def _only_row(session) -> JB2Outbox:
    rows = session.query(JB2Outbox).all()
    assert len(rows) == 1
    return rows[0]
