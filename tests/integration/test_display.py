"""Integration tests for P1-13 display feeds (CR-011) -- DD §4.2 display row.

Zero network: fake-JB2 (tests/conftest.py's `fake_jb2` fixture) + SQLite
in-memory, same portable-types trick as tests/integration/test_sync.py.
Covers: a 504/error on one display feed is recorded but never affects the
other display feed or any regular sync resource; the `/api/v1/jb2-*`
endpoints serve the last-good cached payload even once JB2 stops answering.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import get_session
from app.domain.models_jb2 import Base, DisplayCache, JB2Order, SyncRun
from app.jb2.client import Jb2Client
from app.main import app
from app.sync.display import DISPLAY_REGISTRY, run_display_cycle
from app.sync.worker import REGISTRY, run_all_due


class _SyncASGITransport(httpx.BaseTransport):
    """ponytail: same test-only sync/async bridge as test_sync.py -- Jb2Client
    is sync, the fake-JB2 ASGI app is async-only."""

    def __init__(self, asgi_transport: httpx.ASGITransport) -> None:
        self._inner = asgi_transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _drain() -> httpx.Response:
            response = await self._inner.handle_async_request(request)
            await response.aread()
            return response

        drained = asyncio.run(_drain())
        return httpx.Response(
            status_code=drained.status_code, headers=drained.headers,
            content=drained.content, request=request,
        )


@pytest.fixture
def db_session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def jb2_client(fake_jb2):
    transport, state = fake_jb2
    client = Jb2Client(
        "http://fake-jb2", "http://fake-jb2", "test-client-id", "test-client-secret",
        transport=_SyncASGITransport(transport),
        sleeper=lambda s: None,
    )
    yield client, state
    client.close()


SHOPVIEW_DEF = next(rd for rd in DISPLAY_REGISTRY if rd.name == "jb2-shopview")
SCHEDULE_DEF = next(rd for rd in DISPLAY_REGISTRY if rd.name == "jb2-schedule")


def _order(unique_id: int, order_number: str, last_mod: str) -> dict:
    return {
        "orderNumber": order_number, "customerCode": "AGW STOCK",
        "customerDescription": "AGW Stock", "status": "Open",
        "uniqueID": unique_id, "lastModDate": last_mod,
    }


def test_display_cycle_caches_payload(jb2_client, db_session_factory):
    client, state = jb2_client
    state["shopview/get-jobs"] = [{"JobNo": "10008-01", "OperationStatus": "Running"}]

    with db_session_factory() as session:
        run = run_display_cycle(session, client, SHOPVIEW_DEF)
        session.commit()

        assert run.error is None
        row = session.get(DisplayCache, "jb2-shopview")
        assert row is not None
        assert row.payload == [{"JobNo": "10008-01", "OperationStatus": "Running"}]


def test_schedule_feed_keeps_its_nonstandard_envelope(jb2_client, db_session_factory):
    client, state = jb2_client
    state["eci-aps/get-schedule"] = {
        "StartDateProject": "07/23/2024 16:08",
        "EndDateProject": "04/27/2029 14:34",
        "Data": [{"TaskId": 1, "WorkCenter": "BLAST"}],
    }

    with db_session_factory() as session:
        run_display_cycle(session, client, SCHEDULE_DEF)
        session.commit()

        row = session.get(DisplayCache, "jb2-schedule")
        # unwrap=False -- the whole {StartDateProject, EndDateProject, Data}
        # envelope is cached, not just the unwrapped Data array (findings §1).
        assert row.payload["StartDateProject"] == "07/23/2024 16:08"
        assert row.payload["Data"] == [{"TaskId": 1, "WorkCenter": "BLAST"}]


def test_display_feed_504_isolated_from_other_display_feed_and_other_sync(
    jb2_client, db_session_factory
):
    client, state = jb2_client
    state["shopview/get-jobs"] = [{"JobNo": "10008-01"}]
    state["eci-aps/get-schedule"] = {"StartDateProject": "", "EndDateProject": "", "Data": []}
    state["orders"] = [_order(30, "10008", "2023-10-25T14:30:13Z")]

    # shopview 504s enough times to exhaust the client's own retry budget
    # (MAX_RETRIES=2 -> 3 total attempts); schedule and regular sync are
    # unaffected.
    state["inject"][("GET", "/api/v1/shopview/get-jobs")] = [
        {"status": 504, "body": "upstream request timeout"} for _ in range(3)
    ]

    with db_session_factory() as session:
        display_runs = run_all_due(
            session, client, DISPLAY_REGISTRY, {}, clock=lambda: 0.0, run_fn=run_display_cycle
        )
        by_resource = {r.resource: r for r in display_runs}

        assert by_resource["jb2-shopview"].error is not None
        assert by_resource["jb2-schedule"].error is None
        assert session.get(DisplayCache, "jb2-shopview") is None
        assert session.get(DisplayCache, "jb2-schedule") is not None

        # A failing display feed never touches regular mirror sync.
        orders_def = next(rd for rd in REGISTRY if rd.name == "orders")
        mirror_runs = run_all_due(session, client, [orders_def], {}, clock=lambda: 0.0)
        assert mirror_runs[0].error is None
        assert session.query(JB2Order).count() == 1
        assert session.query(SyncRun).count() == 3  # 2 display + 1 orders


def test_display_cache_survives_a_later_failure(jb2_client, db_session_factory):
    """Last-good payload is served even once JB2 stops answering -- the
    cache row is only ever overwritten on success, never cleared on error."""
    client, state = jb2_client
    state["shopview/get-jobs"] = [{"JobNo": "10008-01"}]

    with db_session_factory() as session:
        run_display_cycle(session, client, SHOPVIEW_DEF)
        session.commit()
        assert session.get(DisplayCache, "jb2-shopview").payload == [{"JobNo": "10008-01"}]

    state["inject"][("GET", "/api/v1/shopview/get-jobs")] = [
        {"status": 504, "body": "upstream request timeout"} for _ in range(3)
    ]

    with db_session_factory() as session:
        run = run_display_cycle(session, client, SHOPVIEW_DEF)
        session.commit()

        assert run.error is not None
        # Stale-but-good payload is still there.
        assert session.get(DisplayCache, "jb2-shopview").payload == [{"JobNo": "10008-01"}]


def test_jb2_schedule_endpoint_serves_cached_payload(db_session_factory):
    session_factory = db_session_factory
    engine = session_factory.kw["bind"]

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    try:
        with Session(engine) as session:
            session.add(DisplayCache(
                key="jb2-schedule",
                payload={"StartDateProject": "x", "Data": []},
                fetched_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            ))
            session.commit()

        with TestClient(app) as c:
            resp = c.get("/api/v1/jb2-schedule")
            assert resp.status_code == 200
            body = resp.json()
            assert body["payload"] == {"StartDateProject": "x", "Data": []}

            # Never cached -- 503, not a crash.
            resp2 = c.get("/api/v1/jb2-shopview")
            assert resp2.status_code == 503
    finally:
        del app.dependency_overrides[get_session]
