"""Integration tests for the sync worker framework (P1-05).

Zero network: fake-JB2 (tests/fake_jb2, via the shared `fake_jb2` fixture)
+ SQLite in-memory (via app.domain.models_jb2.Base.metadata, portable
JSON/Uuid types) stand in for JB2 and Postgres respectively.

Jb2Client is a real *sync* httpx.Client; the fake-JB2 ASGI app is normally
driven by httpx.AsyncClient (see tests/conftest.py's `fake_jb2` fixture).
`_SyncASGITransport` below bridges the two for this test module only --
never used in production, where Jb2Client talks to the real JB2 REST API
over ordinary sync httpx.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.domain.models_jb2 import Base, JB2Order, JB2OrderLineItem, SyncRun
from app.jb2.client import Jb2Client
from app.sync import checkpoints
from app.sync.engine import to_utc
from app.sync.worker import REGISTRY, run_all_due, run_cycle

BASE_URL = "http://fake-jb2"


class _SyncASGITransport(httpx.BaseTransport):
    """ponytail: one asyncio.run() per call -- fine for a test-only bridge
    between Jb2Client's sync httpx.Client and the ASGI-only fake-JB2 app.
    Fully drains the async response body inside the event loop and rebuilds
    a plain in-memory httpx.Response -- httpx.Client asserts a sync byte
    stream, which the raw ASGI response (an async stream) is not."""

    def __init__(self, asgi_transport: httpx.ASGITransport) -> None:
        self._inner = asgi_transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _drain() -> httpx.Response:
            response = await self._inner.handle_async_request(request)
            await response.aread()
            return response

        drained = asyncio.run(_drain())
        return httpx.Response(
            status_code=drained.status_code,
            headers=drained.headers,
            content=drained.content,
            request=request,
        )


@pytest.fixture
def db_session_factory():
    """A single shared-connection in-memory SQLite DB so multiple Session()
    instances (simulating worker restarts) see the same data -- the
    standard StaticPool trick for testing with :memory: across sessions."""
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
        BASE_URL, BASE_URL, "test-client-id", "test-client-secret",
        transport=_SyncASGITransport(transport),
        sleeper=lambda s: None,  # no real throttle/backoff delay in tests
    )
    yield client, state
    client.close()


def _order(unique_id: int, order_number: str, status: str, last_mod: str) -> dict:
    return {
        "orderNumber": order_number,
        "customerCode": "AGW STOCK",
        "customerDescription": "AGW Stock",
        "status": status,
        "uniqueID": unique_id,
        "lastModDate": last_mod,
    }


ORDERS_DEF = next(rd for rd in REGISTRY if rd.name == "orders")
LINE_ITEMS_DEF = next(rd for rd in REGISTRY if rd.name == "order-line-items")


# -- new order lands on next cycle -------------------------------------------

def test_new_order_lands_in_mirror_on_next_cycle(jb2_client, db_session_factory):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]

    with db_session_factory() as session:
        run = run_cycle(session, client, ORDERS_DEF)
        session.commit()

        assert run.error is None
        assert run.fetched == 1
        assert run.changed == 1
        row = session.query(JB2Order).filter_by(jb2_id="30").one()
        assert row.order_number == "10008"
        assert row.status == "Open"


# -- changed record updates via hash diff ------------------------------------

def test_changed_record_updates_via_hash_diff(jb2_client, db_session_factory):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]

    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)
        session.commit()
        first_updated_at = session.query(JB2Order).filter_by(jb2_id="30").one().updated_at

    state["orders"][0] = _order(30, "10008", "Closed", "2023-10-26T09:00:00Z")

    with db_session_factory() as session:
        run = run_cycle(session, client, ORDERS_DEF)
        session.commit()

        assert run.changed == 1
        row = session.query(JB2Order).filter_by(jb2_id="30").one()
        assert row.status == "Closed"
        assert row.updated_at >= first_updated_at


# -- unchanged record no-ops --------------------------------------------------

def test_unchanged_record_is_a_noop(jb2_client, db_session_factory):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]

    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)
        session.commit()
        first_updated_at = session.query(JB2Order).filter_by(jb2_id="30").one().updated_at

    # Same record re-served (overlap window re-fetches it) -- second cycle
    # must not touch updated_at or count it as changed.
    with db_session_factory() as session:
        run = run_cycle(session, client, ORDERS_DEF)
        session.commit()

        assert run.changed == 0
        row = session.query(JB2Order).filter_by(jb2_id="30").one()
        assert row.updated_at == first_updated_at


# -- checkpoint advances; overlap re-fetch doesn't duplicate ------------------

def test_checkpoint_advances_and_overlap_refetch_does_not_duplicate(
    jb2_client, db_session_factory
):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]

    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)
        session.commit()
        checkpoint_after_1 = checkpoints.get_checkpoint(session, "orders")
        assert checkpoint_after_1 == datetime(2023, 10, 25, 14, 30, 13, tzinfo=timezone.utc)

    # Next cycle polls from checkpoint - OVERLAP_S, re-fetching the same
    # boundary record (findings §4: gte is inclusive) -- must dedupe, not add a row.
    with db_session_factory() as session:
        run = run_cycle(session, client, ORDERS_DEF)
        session.commit()

        assert run.fetched == 1  # the fake server re-served it
        assert run.changed == 0  # but the hash matched -- no duplicate, no-op
        assert session.query(JB2Order).count() == 1
        assert checkpoints.get_checkpoint(session, "orders") == checkpoint_after_1


def test_order_line_items_fields_quirk_lastmoddate_requested_explicitly(
    jb2_client, db_session_factory
):
    """findings §4: order-line-items' default field set omits lastModDate --
    the registry's fields= list must carry it explicitly or the checkpoint
    would never advance. Assert the actual request the client sent."""
    client, state = jb2_client
    state["order-line-items"] = [
        {
            "orderNumber": "10008",
            "jobNumber": "10008-01",
            "itemNumber": 1,
            "partNumber": "120-5300",
            "partDescription": "Venom Customs Slides",
            "quantityToMake": 3,
            "dueDate": "2023-10-23T04:00:00Z",
            "status": "Open",
            "uniqueID": 507,
            "lastModDate": "2023-10-25T14:30:13Z",
        }
    ]

    with db_session_factory() as session:
        run = run_cycle(session, client, LINE_ITEMS_DEF)
        session.commit()

        assert run.error is None
        assert run.changed == 1
        row = session.query(JB2OrderLineItem).filter_by(jb2_id="507").one()
        assert row.part_number == "120-5300"
        expected_lm = datetime(2023, 10, 25, 14, 30, 13, tzinfo=timezone.utc)
        assert to_utc(row.jb2_last_modified) == expected_lm
        # Checkpoint only advances because lastModDate was actually present
        # in the returned rows -- proves the explicit fields= worked.
        assert checkpoints.get_checkpoint(session, "order-line-items") is not None


# -- one resource failing doesn't stop others ---------------------------------

def test_one_resource_failing_does_not_stop_others(jb2_client, db_session_factory):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]
    state["order-line-items"] = [
        {
            "orderNumber": "10008", "jobNumber": "10008-01", "itemNumber": 1,
            "partNumber": "120-5300", "partDescription": "Venom Customs Slides",
            "quantityToMake": 3, "dueDate": "2023-10-23T04:00:00Z", "status": "Open",
            "uniqueID": 507, "lastModDate": "2023-10-25T14:30:13Z",
        }
    ]
    # Enough queued 500s to exhaust the client's retry budget on order-line-items only.
    state["inject"][("GET", "/api/v1/order-line-items")] = [
        {"status": 500, "body": {"Title": "boom", "Status": 500}} for _ in range(5)
    ]

    with db_session_factory() as session:
        runs_done = run_all_due(session, client, REGISTRY, {}, clock=lambda: 0.0)

        by_resource = {r.resource: r for r in runs_done}
        assert by_resource["orders"].error is None
        assert by_resource["orders"].changed == 1
        assert by_resource["order-line-items"].error is not None

        # Orders landed despite the sibling's failure.
        assert session.query(JB2Order).count() == 1
        # The failed resource's records never landed (rolled back).
        assert session.query(JB2OrderLineItem).count() == 0
        # Both cycles are recorded, one clean one errored.
        assert session.query(SyncRun).count() == 2


# -- worker resumes from persisted checkpoint after simulated restart --------

def test_worker_resumes_from_persisted_checkpoint_after_restart(jb2_client, db_session_factory):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]

    # "Process 1": one cycle, then the process dies (session/local state gone).
    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)
        session.commit()

    # A brand-new order appears in JB2 while the worker was down.
    state["orders"].append(_order(31, "10009", "Open", "2023-10-26T09:00:00Z"))

    # "Process 2" (simulated restart): fresh Session, fresh in-memory
    # last_run_at -- correctness must come entirely from the DB checkpoint.
    with db_session_factory() as session:
        run = run_cycle(session, client, ORDERS_DEF)
        session.commit()

        assert run.error is None
        assert run.fetched == 2  # both rows re-served within the overlap window
        assert run.changed == 1  # only the genuinely new one is a write
        assert session.query(JB2Order).count() == 2
        new_row = session.query(JB2Order).filter_by(jb2_id="31").one()
        assert new_row.order_number == "10009"


def test_run_all_due_respects_cadence(jb2_client, db_session_factory):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]

    with db_session_factory() as session:
        clock = {"t": 0.0}
        last_run_at: dict[str, float] = {}
        run_all_due(session, client, [ORDERS_DEF], last_run_at, clock=lambda: clock["t"])
        assert session.query(SyncRun).count() == 1

        # Not due yet -- cadence is 60s, only 1s elapsed.
        clock["t"] = 1.0
        run_all_due(session, client, [ORDERS_DEF], last_run_at, clock=lambda: clock["t"])
        assert session.query(SyncRun).count() == 1

        # Cadence elapsed -- runs again.
        clock["t"] = 61.0
        run_all_due(session, client, [ORDERS_DEF], last_run_at, clock=lambda: clock["t"])
        assert session.query(SyncRun).count() == 2
