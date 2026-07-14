"""Sync worker framework (P1-05/06/07/08, DD §4.2, docs/jb2-api-findings.md §4).

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

P1-06/07 add change classification for orders/order-line-items: `order_new`,
`order_changed`, `order_closed`, `order_line_item_new`,
`order_line_item_changed` are logged and fired through `register_order_hook`
(Phase 2's work-order creation subscribes there — no-op by default). A
new/changed order-line-item also triggers its routing + planned-materials
fetch (P1-07), synchronously, within the same order-line-items cycle — not
a separate cadence.

P1-08 adds registry entries for the 15-min master resources (parts,
work-centers, operation-codes, employees, reason-codes, documents). A couple
of them (estimates, document-histories) have no `lastModDate` at all in the
live sample — those set `supports_last_mod=False` and just do a windowed
full pull every cycle, relying on `upsert_records`' hash diff to no-op
unchanged rows (engine already supports this, DD §4.2).
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.models_jb2 import (
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
from app.sync import checkpoints, runs
from app.sync.engine import content_hash, format_jb2_datetime, parse_jb2_datetime, upsert_records

logger = logging.getLogger("app.sync.worker")

# Sentinel floor for a resource's first-ever pull (no checkpoint yet).
EPOCH_FLOOR = datetime(1970, 1, 1, tzinfo=timezone.utc)

# -- P1-06: order/line-item change classification hook point -----------------

# Phase 2 (work-order creation) subscribes here to react to
# order_new/order_changed/order_closed/order_line_item_new/
# order_line_item_changed events -- no-op by default (nothing registered).
OrderHook = Callable[[str, dict[str, Any]], None]
_order_hooks: list[OrderHook] = []

# DD §4.3 rule 4: an order in one of these JB2 statuses (case-insensitive)
# is canceled/closed -- work-order cancellation reacts to the transition.
CLOSED_STATUSES = {"closed", "canceled", "cancelled"}


def register_order_hook(hook: OrderHook) -> None:
    """Register a callback invoked as `hook(event, raw_record)` for every
    order/order-line-item classification event. Additive -- callers never
    need to unregister in production (Phase 2 registers once at startup);
    tests that register a hook should pop it from `_order_hooks` after."""
    _order_hooks.append(hook)


def _fire_order_event(event: str, record: dict[str, Any]) -> None:
    logger.info(
        event,
        extra={
            "sync_resource": "orders",
            "jb2_order_number": record.get("orderNumber"),
            "jb2_unique_id": record.get("uniqueID"),
        },
    )
    for hook in _order_hooks:
        hook(event, record)


def _status_is_closed(status: Any) -> bool:
    return bool(status) and str(status).strip().lower() in CLOSED_STATUSES


def _diff_page(
    session: Session, model: type, records: list[dict[str, Any]],
    *, key_field: str = "uniqueID", key_attr: str = "jb2_id",
) -> dict[str, str]:
    """Classify each record as "new" or "changed" (content hash differs from
    what's currently stored) *before* `upsert_records` mutates the mirror --
    unchanged records are simply absent from the returned mapping (mirrors
    upsert_records' own no-op skip, so callers never double-guess it)."""
    ids = [str(r[key_field]) for r in records]
    existing_hashes = {
        getattr(row, key_attr): row.content_hash
        for row in session.scalars(select(model).where(getattr(model, key_attr).in_(ids)))
    }
    classification: dict[str, str] = {}
    for record in records:
        jb2_id = str(record[key_field])
        if jb2_id not in existing_hashes:
            classification[jb2_id] = "new"
        elif existing_hashes[jb2_id] != content_hash(record):
            classification[jb2_id] = "changed"
    return classification


def _emit_order_events(page: list[dict[str, Any]], classification: dict[str, str]) -> None:
    for record in page:
        kind = classification.get(str(record.get("uniqueID")))
        if kind is None:
            continue
        if kind == "new":
            _fire_order_event("order_new", record)
        elif _status_is_closed(record.get("status")):
            _fire_order_event("order_closed", record)
        else:
            _fire_order_event("order_changed", record)


def _emit_line_item_events(
    session: Session, client: Jb2Client, page: list[dict[str, Any]],
    classification: dict[str, str], now: Callable[[], datetime],
) -> None:
    for record in page:
        jb2_id = str(record.get("uniqueID"))
        kind = classification.get(jb2_id)
        if kind is None:
            continue
        _fire_order_event(
            "order_line_item_new" if kind == "new" else "order_line_item_changed", record
        )
        # P1-07: fetch this line item's routing + planned materials, keyed
        # off the row we (or the upsert above) just wrote.
        line_item = session.scalars(
            select(JB2OrderLineItem).where(JB2OrderLineItem.jb2_id == jb2_id)
        ).one_or_none()
        if line_item is not None:
            _sync_line_item_children(session, client, line_item.id, record.get("jobNumber"), now)


# -- P1-07: order-routings + job-materials/job-requirements follow-on --------

ROUTING_FIELDS = [
    "stepNumber", "operationCode", "description", "workCenter",
    "totalEstimatedHours", "uniqueID", "lastModDate", "jobNumber", "orderNumber",
]
JOB_MATERIAL_FIELDS = [
    "stepNumber", "partNumber", "description", "quantityPosted1", "stockUnit",
    "stockingCost", "uniqueID", "lastModDate", "jobNumber", "orderNumber",
]
JOB_REQUIREMENT_FIELDS = [
    "stepNumber", "partNumber", "partDescription", "quantityToBuy", "purchaseUnit",
    "cost", "uniqueID", "lastModDate", "jobNumber", "orderNumber",
]


def _routing_extract_factory(line_item_id: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def extract(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "jb2_line_item_id": line_item_id,
            "seq": record.get("stepNumber"),
            "operation_code": record.get("operationCode"),
            "description": record.get("description"),
            "work_center_code": record.get("workCenter"),
            # ponytail: the live order-routings sample carries no discrete
            # setup-vs-run hour split (findings §4/§4.7.3) -- only a single
            # totalEstimatedHours. Split this once JB2 exposes setupTime/
            # cycleTime here the way it does on operation-codes.
            "est_setup_hrs": None,
            "est_run_hrs": record.get("totalEstimatedHours"),
        }

    return extract


def _job_material_extract_factory(line_item_id: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def extract(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "jb2_line_item_id": line_item_id,
            "routing_seq": record.get("stepNumber"),
            "part_number": record.get("partNumber"),
            "description": record.get("description"),
            "qty_planned": record.get("quantityPosted1"),
            "unit": record.get("stockUnit"),
            "unit_cost": record.get("stockingCost"),
        }

    return extract


def _job_requirement_extract_factory(
    line_item_id: Any,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def extract(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "jb2_line_item_id": line_item_id,
            "routing_seq": record.get("stepNumber"),
            "part_number": record.get("partNumber"),
            "description": record.get("partDescription"),
            "qty_planned": record.get("quantityToBuy"),
            "unit": record.get("purchaseUnit"),
            "unit_cost": record.get("cost"),
        }

    return extract


def _prefix_records(records: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    """job-materials and job-requirements each mint their own `uniqueID`
    namespace but land in the same `jb2_order_materials` mirror -- prefix so
    the two sources never collide on `jb2_id`."""
    return [{**r, "_mirror_uid": f"{prefix}:{r['uniqueID']}"} for r in records]


def _sync_line_item_children(
    session: Session, client: Jb2Client, line_item_id: Any, job_number: str | None,
    now: Callable[[], datetime],
) -> None:
    """P1-07: pull one job's routing + planned materials and upsert them,
    linked by FK to the line item. Triggered from the order-line-items
    cycle itself (no separate cadence). Idempotent: upsert_records
    hash-diffs, so re-running for an unchanged job is a no-op."""
    if not job_number:
        return

    routings = client.get(
        "/order-routings", params={"jobNumber[eq]": job_number}, fields=ROUTING_FIELDS, take=200,
    )
    upsert_records(
        session, JB2OrderRouting, routings, key_field="uniqueID",
        extract=_routing_extract_factory(line_item_id), now=now,
    )

    materials = client.get(
        "/job-materials", params={"jobNumber[eq]": job_number},
        fields=JOB_MATERIAL_FIELDS, take=200,
    )
    upsert_records(
        session, JB2OrderMaterial, _prefix_records(materials, "jm"), key_field="_mirror_uid",
        extract=_job_material_extract_factory(line_item_id), now=now,
    )

    requirements = client.get(
        "/job-requirements", params={"jobNumber[eq]": job_number},
        fields=JOB_REQUIREMENT_FIELDS, take=200,
    )
    upsert_records(
        session, JB2OrderMaterial, _prefix_records(requirements, "jr"), key_field="_mirror_uid",
        extract=_job_requirement_extract_factory(line_item_id), now=now,
    )


# -- P1-05/06 extractors: orders, order-line-items ----------------------------


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


# -- P1-08 extractors: masters (15 min cadence) -------------------------------


def _estimates_extract(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "part_number": record.get("partNumber"),
        "description": record.get("description"),
        "revision": record.get("revision"),
    }


def _work_centers_extract(record: dict[str, Any]) -> dict[str, Any]:
    code = record.get("workCenter")
    return {
        "code": str(code) if code is not None else None,
        "name": record.get("description"),
    }


def _operation_codes_extract(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": record.get("description"),
        # ponytail: operation-codes carries no work-center reference in the
        # live sample -- that link comes via order-routings/work-centers
        # instead. Revisit if JB2 ever adds one to this resource.
        "work_center_code": None,
    }


def _employees_extract(record: dict[str, Any]) -> dict[str, Any]:
    code = record.get("employeeCode")
    return {
        "employee_code": str(code) if code is not None else None,
        "name": record.get("employeeName"),
        "active": record.get("active"),
    }


def _reason_codes_extract(record: dict[str, Any]) -> dict[str, Any]:
    return {"description": record.get("description")}


def _document_controls_extract(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_number": record.get("documentNumber"),
        "revision": record.get("revision"),
        "linked_part_number": None,
    }


def _document_histories_extract(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_number": record.get("documentNumber"),
        "revision": record.get("revision"),
        "linked_part_number": None,
    }


@dataclass(frozen=True)
class ResourceDef:
    name: str  # sync_runs / checkpoint key, also the JB2 fake-server resource name
    endpoint: str  # jb2 client path, e.g. "/orders"
    cadence_s: float
    fields: list[str]  # explicit fields= list -- see order-line-items note below
    model: type
    extract: Callable[[dict[str, Any]], dict[str, Any]]
    key_field: str = "uniqueID"  # raw JB2 field carrying the record's unique id
    key_attr: str = "jb2_id"  # model column the key maps to
    # P1-08: a couple of masters (operation-codes, reason-codes) key on
    # their own natural column instead of a generic jb2_id -- key_attr above
    # already carries that; key_prefix is for masters that share a mirror
    # table across two source resources (document-controls/-histories).
    key_prefix: str | None = None
    # P1-08: some masters (estimates, document-histories) have no
    # lastModDate at all -- skip the filter and do a windowed full pull,
    # relying on upsert_records' hash diff to no-op unchanged rows.
    supports_last_mod: bool = True


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
    # -- P1-08 masters, 15 min cadence -----------------------------------
    ResourceDef(
        name="estimates",
        endpoint="/estimates",
        cadence_s=900,
        fields=["partNumber", "description", "revision", "uniqueID"],
        model=JB2Part,
        extract=_estimates_extract,
        supports_last_mod=False,  # findings: no lastModDate on this resource
    ),
    ResourceDef(
        name="work-centers",
        endpoint="/work-centers",
        cadence_s=900,
        fields=["workCenter", "description", "uniqueID", "lastModDate"],
        model=JB2WorkCenter,
        extract=_work_centers_extract,
    ),
    ResourceDef(
        name="operation-codes",
        endpoint="/operation-codes",
        cadence_s=900,
        fields=["operationCode", "description", "uniqueID", "lastModDate"],
        model=JB2OperationCode,
        extract=_operation_codes_extract,
        key_field="operationCode",
        key_attr="code",
    ),
    ResourceDef(
        name="employees",
        endpoint="/employees",
        cadence_s=900,
        fields=["employeeCode", "employeeName", "active", "uniqueID", "lastModDate"],
        model=JB2Employee,
        extract=_employees_extract,
    ),
    ResourceDef(
        name="reason-codes",
        endpoint="/reason-codes",
        cadence_s=900,
        # findings §7: map failure_codes.jb2_reason_number on reasonCodeID,
        # not uniqueID or the string reasonCode.
        fields=["reasonCode", "reasonCodeID", "description", "uniqueID", "lastModDate"],
        model=JB2ReasonCode,
        extract=_reason_codes_extract,
        key_field="reasonCodeID",
        key_attr="reason_number",
    ),
    ResourceDef(
        name="document-controls",
        endpoint="/document-controls",
        cadence_s=900,
        fields=["documentNumber", "revision", "uniqueID", "lastModDate"],
        model=JB2Document,
        extract=_document_controls_extract,
        key_prefix="dc",
    ),
    ResourceDef(
        name="document-histories",
        endpoint="/document-histories",
        cadence_s=900,
        fields=["documentNumber", "revision", "uniqueID"],
        model=JB2Document,
        extract=_document_histories_extract,
        key_prefix="dh",
        supports_last_mod=False,  # findings: no lastModDate on this resource
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
        params = (
            {"lastModDate[gte]": format_jb2_datetime(since)}
            if resource_def.supports_last_mod
            else {}
        )
        for page in client.iter_pages(resource_def.endpoint, params, fields=resource_def.fields):
            if resource_def.name == "orders":
                classification = _diff_page(session, JB2Order, page)
            elif resource_def.name == "order-line-items":
                classification = _diff_page(session, JB2OrderLineItem, page)
            else:
                classification = {}

            if resource_def.key_prefix:
                keyed_page = [
                    {**r, "_prefixed_id": f"{resource_def.key_prefix}:{r[resource_def.key_field]}"}
                    for r in page
                ]
                effective_key_field = "_prefixed_id"
            else:
                keyed_page = page
                effective_key_field = resource_def.key_field

            fetched, changed = upsert_records(
                session,
                resource_def.model,
                keyed_page,
                key_field=effective_key_field,
                key_attr=resource_def.key_attr,
                extract=resource_def.extract,
                now=now,
            )
            fetched_total += fetched
            changed_total += changed

            if resource_def.name == "orders":
                _emit_order_events(page, classification)
            elif resource_def.name == "order-line-items":
                _emit_line_item_events(session, client, page, classification, now)

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
    registry: list[Any],
    last_run_at: dict[str, float],
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    run_fn: Callable[..., SyncRun] = run_cycle,
) -> list[SyncRun]:
    """Run one cycle for every resource whose cadence has elapsed, committing
    after each so one resource's rollback never discards another's work.

    `run_fn` defaults to `run_cycle` (mirror resources); P1-13's display
    feeds reuse this same cadence/commit-isolation machinery with
    `run_fn=app.sync.display.run_display_cycle` against their own registry.
    """
    completed = []
    for resource_def in registry:
        last = last_run_at.get(resource_def.name, float("-inf"))
        if clock() - last < resource_def.cadence_s:
            continue
        run = run_fn(session, client, resource_def, now=now)
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
        display_registry: list[Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._registry = registry
        self._poll_interval_s = poll_interval_s
        self._clock = clock
        self._sleeper = sleeper
        self._last_run_at: dict[str, float] = {}
        self._stop = False
        # P1-13: display feeds poll+cache on their own cadence/registry, run
        # isolated from (but interleaved with) the regular mirror registry.
        if display_registry is None:
            from app.sync.display import DISPLAY_REGISTRY

            display_registry = DISPLAY_REGISTRY
        self._display_registry = display_registry
        self._display_last_run_at: dict[str, float] = {}

    def request_stop(self, *_args: Any) -> None:
        self._stop = True

    def run_forever(self) -> None:
        from app.sync.display import run_display_cycle

        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        logger.info("sync_worker_started")
        while not self._stop:
            with self._session_factory() as session:
                run_all_due(session, self._client, self._registry, self._last_run_at,
                            clock=self._clock)
                run_all_due(session, self._client, self._display_registry,
                            self._display_last_run_at, clock=self._clock, run_fn=run_display_cycle)
            if not self._stop:
                self._sleeper(self._poll_interval_s)
        logger.info("sync_worker_stopped")
