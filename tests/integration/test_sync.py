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

from app.domain.models_jb2 import (
    Base,
    JB2Document,
    JB2Employee,
    JB2OperationCode,
    JB2Order,
    JB2OrderLineItem,
    JB2OrderMaterial,
    JB2OrderRouting,
    JB2Part,
    JB2ReasonCode,
    JB2WorkCenter,
    SyncRun,
)
from app.jb2.client import Jb2Client
from app.sync import checkpoints
from app.sync import worker as worker_module
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
        # Just the two resources this test cares about -- REGISTRY has grown
        # (P1-08 masters) since this test was written and isolation across
        # the *whole* registry isn't this test's concern.
        runs_done = run_all_due(
            session, client, [ORDERS_DEF, LINE_ITEMS_DEF], {}, clock=lambda: 0.0
        )

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


# -- P1-06: order/line-item change classification + hook point --------------

@pytest.fixture
def captured_events():
    """Register a hook capturing (event, record) pairs, popping it on
    teardown -- worker._order_hooks is a module-level list shared across
    tests, so it can't be left registered after this test ends."""
    events: list[tuple[str, dict]] = []

    def hook(event, record):
        events.append((event, record))

    worker_module.register_order_hook(hook)
    yield events
    worker_module._order_hooks.remove(hook)


def test_order_lifecycle_events_fire_new_changed_closed(
    jb2_client, db_session_factory, captured_events
):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]

    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)
        session.commit()

    state["orders"][0] = _order(30, "10008", "In Process", "2023-10-26T09:00:00Z")
    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)
        session.commit()

    state["orders"][0] = _order(30, "10008", "Closed", "2023-10-27T09:00:00Z")
    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)
        session.commit()

    kinds = [event for event, _record in captured_events]
    assert kinds == ["order_new", "order_changed", "order_closed"]


def test_unchanged_order_fires_no_event(jb2_client, db_session_factory, captured_events):
    client, state = jb2_client
    state["orders"] = [_order(30, "10008", "Open", "2023-10-25T14:30:13Z")]

    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)
        session.commit()
    with db_session_factory() as session:
        run_cycle(session, client, ORDERS_DEF)  # overlap re-fetch, byte-identical
        session.commit()

    assert [event for event, _ in captured_events] == ["order_new"]


# -- P1-07: routing + planned-materials follow-on on line-item change -------

def _line_item(unique_id: int, job_number: str, last_mod: str) -> dict:
    return {
        "orderNumber": "10008", "jobNumber": job_number, "itemNumber": 1,
        "partNumber": "120-5300", "partDescription": "Venom Customs Slides",
        "quantityToMake": 3, "dueDate": "2023-10-23T04:00:00Z", "status": "Open",
        "uniqueID": unique_id, "lastModDate": last_mod,
    }


def _routing(job_number: str, step: int, last_mod: str) -> dict:
    return {
        "stepNumber": step, "operationCode": "COATING", "description": "Finish Coating",
        "workCenter": "FINISHES", "totalEstimatedHours": 1.5, "uniqueID": 131072 + step,
        "lastModDate": last_mod, "jobNumber": job_number, "orderNumber": "10008",
    }


def _job_material(job_number: str, last_mod: str) -> dict:
    return {
        "stepNumber": 0, "partNumber": "100-0140", "description": "17-4 PG ROD",
        "quantityPosted1": 9.0, "stockUnit": "BAR", "stockingCost": 0.3001,
        "uniqueID": 36, "lastModDate": last_mod, "jobNumber": job_number,
        "orderNumber": "10008",
    }


def _job_requirement(job_number: str, last_mod: str) -> dict:
    return {
        "stepNumber": 10, "partNumber": "260-2006.1", "partDescription": "Finish Coating",
        "quantityToBuy": 5000.0, "purchaseUnit": "EA", "cost": 0.0,
        "uniqueID": 6213, "lastModDate": last_mod, "jobNumber": job_number,
        "orderNumber": "10008",
    }


def test_new_line_item_triggers_routing_and_materials_fetch_and_is_idempotent(
    jb2_client, db_session_factory
):
    client, state = jb2_client
    job_number = "10008-01"
    state["order-line-items"] = [_line_item(507, job_number, "2023-10-25T14:30:13Z")]
    state["order-routings"] = [_routing(job_number, 60, "2025-03-07T17:06:28Z")]
    state["job-materials"] = [_job_material(job_number, "2023-12-08T15:51:02Z")]
    state["job-requirements"] = [_job_requirement(job_number, "2025-07-29T12:00:20Z")]

    with db_session_factory() as session:
        run = run_cycle(session, client, LINE_ITEMS_DEF)
        session.commit()

        assert run.error is None
        line_item = session.query(JB2OrderLineItem).filter_by(jb2_id="507").one()

        routings = session.query(JB2OrderRouting).all()
        assert len(routings) == 1
        assert routings[0].jb2_line_item_id == line_item.id
        assert routings[0].operation_code == "COATING"

        materials = session.query(JB2OrderMaterial).all()
        assert len(materials) == 2  # one from job-materials, one from job-requirements
        assert {m.part_number for m in materials} == {"100-0140", "260-2006.1"}
        assert all(m.jb2_line_item_id == line_item.id for m in materials)

    # Re-run against the same (unchanged) job -- idempotent, no duplicates.
    with db_session_factory() as session:
        run_cycle(session, client, LINE_ITEMS_DEF)
        session.commit()

        assert session.query(JB2OrderRouting).count() == 1
        assert session.query(JB2OrderMaterial).count() == 2


# -- P1-08: masters (parts, work-centers, operation-codes, employees, --------
# -- reason-codes, documents) at 15-min cadence ------------------------------

def _master_def(name: str):
    return next(rd for rd in REGISTRY if rd.name == name)


def test_masters_sync_populates_all_six_mirrors_and_is_idempotent(
    jb2_client, db_session_factory
):
    client, state = jb2_client
    # Real field shapes lifted from tests/fixtures/jb2/activation-*.json.
    state["estimates"] = [{
        "partNumber": "100-0100", "description": "17-4 PG ROD x .125\" Ground",
        "revision": None, "uniqueID": 1,
    }]
    state["work-centers"] = [{
        "workCenter": 101, "description": "Swiss (SS327)", "uniqueID": 1,
        "lastModDate": "2025-05-15T11:44:10Z",
    }]
    state["operation-codes"] = [{
        "operationCode": "ASSEMBLY", "description": "Small Part Assembly",
        "uniqueID": 36, "lastModDate": "2023-10-03T18:36:48Z",
    }]
    state["employees"] = [{
        "employeeCode": 1, "employeeName": "Adam Nilson", "active": True,
        "uniqueID": 108, "lastModDate": "2024-07-09T12:31:45Z",
    }]
    # reason-codes is already seeded (21 real rows) by seed_from_fixtures().
    state["document-controls"] = [{
        "documentNumber": "110-1001", "revision": "C", "uniqueID": 3,
        "lastModDate": "2024-08-21T15:01:24Z",
    }]
    state["document-histories"] = [{
        "documentNumber": "10046", "revision": "NEW", "uniqueID": 138,
    }]

    master_defs = [
        _master_def(n) for n in (
            "estimates", "work-centers", "operation-codes", "employees",
            "reason-codes", "document-controls", "document-histories",
        )
    ]

    with db_session_factory() as session:
        for rd in master_defs:
            run = run_cycle(session, client, rd)
            session.commit()
            assert run.error is None

        assert session.query(JB2Part).filter_by(part_number="100-0100").count() == 1
        assert session.query(JB2WorkCenter).filter_by(code="101").count() == 1
        assert session.query(JB2OperationCode).filter_by(code="ASSEMBLY").count() == 1
        assert session.query(JB2Employee).filter_by(employee_code="1").count() == 1
        assert session.query(JB2ReasonCode).count() == 21
        # Both document sources land in the same mirror, prefixed apart.
        assert session.query(JB2Document).count() == 2
        assert {d.document_number for d in session.query(JB2Document).all()} == {
            "110-1001", "10046",
        }

    # Repeat runs: fixtures unchanged -> hash diff no-ops everything.
    with db_session_factory() as session:
        for rd in master_defs:
            run = run_cycle(session, client, rd)
            session.commit()
            assert run.changed == 0
        assert session.query(JB2Document).count() == 2
