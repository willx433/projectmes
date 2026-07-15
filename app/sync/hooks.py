"""Wires order/line-item sync events to work-order creation/reconciliation
(P2-10, DD §4.3). Subscribes via `app.sync.worker.register_order_hook` --
kept in its own module (rather than inside `app.sync.worker`) so the sync
framework has zero import-time dependency on the domain layer; only
`app/sync/__main__.py` (the process entry point) imports this module, at
startup, to actually wire the hook up.

The hook receives the *same* `session` the firing sync cycle is using
(still open, not yet committed -- see `app.sync.worker.OrderHook`'s
docstring for why that matters). So work-order creation rides in the exact
same transaction as the mirror row it reacts to: both commit or both roll
back together, same guarantee the outbox gives its own writes (DD §4.5).

`order_line_item_new` / `order_line_item_changed` -> `workorders
.create_from_line_item` (idempotent: creates or reconciles).

`order_closed` fires with the raw JB2 *order* record, not a line item, and
`jb2_order_line_items.jb2_order_id` is never populated (see
app/sync/worker.py's `_order_line_items_extract` -- that cross-resource FK
resolution was explicitly left out of scope in Phase 1, P1-06/07). Standing
resolution here: match this order's `orderNumber` against the raw JB2
`orderNumber` each line-item mirror row still carries in its own `payload`
jsonb (every mirror row keeps the full raw record, see MirrorMixin). This is
a full table scan over `jb2_order_line_items` -- fine at MES/single-shop
scale; flagged as a judgment call rather than adding a real FK, since fixing
the FK gap properly is a Phase 1 concern out of this task's scope.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import workorders
from app.domain.models_execution import WorkOrder
from app.domain.models_jb2 import JB2OrderLineItem
from app.sync.worker import register_order_hook

logger = logging.getLogger("app.sync.hooks")


def _handle_line_item_event(session: Session, record: dict[str, Any]) -> None:
    jb2_id = str(record.get("uniqueID"))
    line_item = session.scalars(
        select(JB2OrderLineItem).where(JB2OrderLineItem.jb2_id == jb2_id)
    ).one_or_none()
    if line_item is None:
        # Shouldn't happen (the mirror upsert for this exact record runs
        # earlier in the same cycle/transaction, see app/sync/worker.py's
        # _emit_line_item_events) but never let a surprise here crash sync.
        logger.warning("hook_line_item_missing", extra={"jb2_id": jb2_id})
        return
    workorders.create_from_line_item(session, line_item)


def _handle_order_closed(session: Session, record: dict[str, Any]) -> None:
    order_number = record.get("orderNumber")
    if not order_number:
        return
    # ponytail: full scan, see module docstring -- jb2_order_id resolution
    # is an unfixed Phase 1 gap, not this task's to close.
    line_items = session.scalars(select(JB2OrderLineItem)).all()
    for line_item in line_items:
        if line_item.payload.get("orderNumber") != order_number:
            continue
        work_order = session.scalars(
            select(WorkOrder).where(WorkOrder.jb2_line_item_id == line_item.id)
        ).one_or_none()
        if work_order is not None:
            workorders.cancel_work_order(session, work_order)


def handle_order_event(event: str, record: dict[str, Any], session: Session) -> None:
    """The callback registered with `register_order_hook`. Any exception
    here propagates to `run_cycle`'s own per-resource try/except (isolation
    already exists there) -- this cycle's mirror upsert + hook work roll
    back together and retry next poll, same as any other cycle failure."""
    if event in ("order_line_item_new", "order_line_item_changed"):
        _handle_line_item_event(session, record)
    elif event == "order_closed":
        _handle_order_closed(session, record)


def register() -> None:
    register_order_hook(handle_order_event)
