"""Sync worker framework (P1-05, DD §4.2, docs/jb2-api-findings.md §4).

`REGISTRY` lists what to sync: resource name, JB2 endpoint, poll cadence,
explicit `fields=` list, model class, and a raw-record -> model-columns
extractor. `run_cycle` runs one poll cycle for a single resource, isolated —
a failing resource records its error onto its own `sync_runs` row and never
touches another resource's session state. `run_all_due` walks the registry
once, running (and committing) whichever resources are due. `SyncWorker` is
the restart-safe main loop (`python -m app.sync`, see __main__.py):
cadence tracking is in-memory only (a restart just makes everything
immediately due again — harmless), because correctness comes from the
persisted checkpoint (checkpoints.py), not from the loop's own memory.
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from app.domain.models_jb2 import JB2Order, JB2OrderLineItem, SyncRun
from app.jb2.client import Jb2Client
from app.sync import checkpoints, runs
from app.sync.engine import format_jb2_datetime, parse_jb2_datetime, upsert_records

logger = logging.getLogger("app.sync.worker")

# Sentinel floor for a resource's first-ever pull (no checkpoint yet).
EPOCH_FLOOR = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _orders_extract(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_number": record.get("orderNumber"),
        "customer": record.get("customerDescription") or record.get("customerCode"),
        "status": record.get("status"),
        "due_date": None,  # orders has no dueDate field -- that lives on line items
    }


def _order_line_items_extract(record: dict[str, Any]) -> dict[str, Any]:
    return {
        # jb2_order_id cross-resource FK resolution lands with P1-06/07.
        "jb2_order_id": None,
        "part_number": record.get("partNumber"),
        "description": record.get("partDescription"),
        "qty": record.get("quantityToMake"),
        "due_date": _date_only(record.get("dueDate")),
    }


def _date_only(value: str | None) -> date | None:
    if not value:
        return None
    return parse_jb2_datetime(value).date()


@dataclass(frozen=True)
class ResourceDef:
    name: str  # sync_runs / checkpoint key, also the JB2 fake-server resource name
    endpoint: str  # jb2 client path, e.g. "/orders"
    cadence_s: float
    fields: list[str]  # explicit fields= list -- see order-line-items note below
    model: type
    extract: Callable[[dict[str, Any]], dict[str, Any]]
    key_field: str = "uniqueID"  # raw JB2 field carrying the record's unique id


REGISTRY: list[ResourceDef] = [
    ResourceDef(
        name="orders",
        endpoint="/orders",
        cadence_s=60,
        fields=["orderNumber", "customerCode", "customerDescription", "status",
                "uniqueID", "lastModDate"],
        model=JB2Order,
        extract=_orders_extract,
    ),
    ResourceDef(
        name="order-line-items",
        endpoint="/order-line-items",
        cadence_s=60,
        # findings §4 quirk: order-line-items' default field set silently
        # omits lastModDate -- must be requested explicitly or the
        # checkpoint never advances (fixture: lastmod-order-line-items-fields.json).
        fields=["orderNumber", "jobNumber", "itemNumber", "partNumber",
                "partDescription", "quantityToMake", "dueDate", "status",
                "uniqueID", "lastModDate"],
        model=JB2OrderLineItem,
        extract=_order_line_items_extract,
    ),
]


def run_cycle(
    session: Session,
    client: Jb2Client,
    resource_def: ResourceDef,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SyncRun:
    """Run one poll cycle for a single resource. Never raises: a failure is
    caught, rolled back (discarding this resource's partial upsert work
    only -- see run_all_due's per-resource commit), and recorded as an
    errored sync_runs row so one resource's outage never blocks siblings."""
    started = now()
    since = checkpoints.poll_since(session, resource_def.name) or EPOCH_FLOOR
    fetched_total = 0
    changed_total = 0
    max_seen = since
    error: str | None = None

    try:
        params = {"lastModDate[gte]": format_jb2_datetime(since)}
        for page in client.iter_pages(resource_def.endpoint, params, fields=resource_def.fields):
            fetched, changed = upsert_records(
                session,
                resource_def.model,
                page,
                key_field=resource_def.key_field,
                extract=resource_def.extract,
                now=now,
            )
            fetched_total += fetched
            changed_total += changed
            for record in page:
                raw_lm = record.get("lastModDate")
                if raw_lm:
                    seen = parse_jb2_datetime(raw_lm)
                    if seen > max_seen:
                        max_seen = seen

        if fetched_total:
            checkpoints.advance_checkpoint(session, resource_def.name, max_seen, now=now())
    except Exception as exc:  # noqa: BLE001 -- isolation is the point (P1-05 AC)
        logger.exception("sync_cycle_failed", extra={"sync_resource": resource_def.name})
        session.rollback()
        error = str(exc)
        fetched_total = 0
        changed_total = 0

    finished = now()
    return runs.record_run(
        session,
        resource=resource_def.name,
        started_at=started,
        finished_at=finished,
        fetched=fetched_total,
        changed=changed_total,
        window_since=since,
        error=error,
    )


def run_all_due(
    session: Session,
    client: Jb2Client,
    registry: list[ResourceDef],
    last_run_at: dict[str, float],
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[SyncRun]:
    """Run one cycle for every resource whose cadence has elapsed, committing
    after each so one resource's rollback never discards another's work."""
    completed = []
    for resource_def in registry:
        last = last_run_at.get(resource_def.name, float("-inf"))
        if clock() - last < resource_def.cadence_s:
            continue
        run = run_cycle(session, client, resource_def, now=now)
        session.commit()
        last_run_at[resource_def.name] = clock()
        completed.append(run)
    return completed


class SyncWorker:
    """Restart-safe main loop. Entry point: `python -m app.sync` (__main__.py)."""

    def __init__(
        self,
        session_factory: sessionmaker,
        client: Jb2Client,
        registry: list[ResourceDef] = REGISTRY,
        *,
        poll_interval_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._registry = registry
        self._poll_interval_s = poll_interval_s
        self._clock = clock
        self._sleeper = sleeper
        self._last_run_at: dict[str, float] = {}
        self._stop = False

    def request_stop(self, *_args: Any) -> None:
        self._stop = True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        logger.info("sync_worker_started")
        while not self._stop:
            with self._session_factory() as session:
                run_all_due(session, self._client, self._registry, self._last_run_at,
                            clock=self._clock)
            if not self._stop:
                self._sleeper(self._poll_interval_s)
        logger.info("sync_worker_stopped")
