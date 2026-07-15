"""Append-only event/audit plumbing (P3-13, DD §9.10, state-machine.md §9).

`emit()` is the ONLY writer in this module -- no update/delete function
exists here by construction, matching Event's docstring (append-only,
DB-level REVOKE UPDATE/DELETE is a separate prod runbook item). Callers
own the session/transaction; `emit()` only `session.add()`s -- it never
commits.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models_floor import Event

# state-machine.md §9 -- the contract. Unknown verbs raise in emit().
VALID_VERBS = frozenset(
    {
        "scan.accepted",
        "scan.rejected",
        "session.opened",
        "session.paused",
        "session.resumed",
        "session.closed",
        # P3-08/09 addition: O6 lead-confirm of an auto_closed session is its
        # own moment (outbox enqueue happens here, not at auto-close) --
        # distinct from the generic "session.closed" that already fired when
        # the session auto-closed.
        "session.confirmed",
        "substep.done",
        "substep.failed",
        "substep.skipped",
        "measurement.recorded",
        "failure.recorded",
        "disposition.applied",
        "unit.moved",
        "unit.reworked",
        "unit.scrapped",
        "unit.done",
        "box.assigned",
        "box.released",
        "wo.status_changed",
        "outbox.enqueued",
        "auth.login",
        "auth.logout",
        "auth.badge_fail",
        "auth.override",
        # P3-03 addition: badge revocation isn't a bare login/logout outcome
        # (it's an admin action against `operators`, no auth_events kind
        # fits per the DD §14 enum) -- logged to the generic timeline instead.
        "auth.badge_revoked",
    }
)


def _json_safe(value: Any) -> Any:
    """Coerce uuid/datetime (recursively, in dicts/lists) to JSON-safe values."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (uuid.UUID, _dt.datetime, _dt.date)):
        return str(value)
    return value


def emit(
    session: Session,
    verb: str,
    *,
    entity: Any,
    actor_id: uuid.UUID | None = None,
    station_id: uuid.UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> Event:
    """Append one Event row to `session` (no commit -- caller's transaction).

    `entity` is either an ORM object (must have `.id`; entity_kind is its
    class name lowercased) or a `(kind, id)` tuple for cases with no ORM
    object at hand (e.g. a bare work-order id).
    """
    if verb not in VALID_VERBS:
        raise ValueError(f"unknown event verb: {verb!r} (see state-machine.md §9)")

    if isinstance(entity, tuple):
        entity_kind, entity_id = entity
    else:
        entity_kind = type(entity).__name__.lower()
        entity_id = entity.id

    row = Event(
        actor_id=actor_id,
        station_id=station_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        verb=verb,
        before=_json_safe(before),
        after=_json_safe(after),
    )
    session.add(row)
    return row


def timeline(
    session: Session,
    *,
    entity_kind: str | None = None,
    entity_id: uuid.UUID | None = None,
    unit_id: uuid.UUID | None = None,
    limit: int = 500,
) -> list[Event]:
    """Chronological (oldest-first) query over `events`.

    `unit_id` is a convenience equivalent to `entity_id=unit_id` -- it does
    NOT reach into other entities' scans/sessions/substeps that merely
    *reference* a unit. Callers wanting a unit's full cross-entity history
    must pass those entities' own ids (e.g. session/substep ids) themselves;
    this function does not search `before`/`after` JSON payloads.
    """
    if unit_id is not None and entity_id is not None:
        raise ValueError("pass entity_id or unit_id, not both")
    if unit_id is not None:
        entity_id = unit_id

    stmt = select(Event).order_by(Event.at.asc(), Event.id.asc())
    if entity_kind is not None:
        stmt = stmt.where(Event.entity_kind == entity_kind)
    if entity_id is not None:
        stmt = stmt.where(Event.entity_id == entity_id)
    stmt = stmt.limit(limit)
    return list(session.scalars(stmt))
