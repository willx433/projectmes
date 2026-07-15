"""Generic mirror upsert engine (P1-05, DD §4.2).

Given a model class, a page of raw JB2 records, and a key field, upsert each
record into the mirror table: insert new rows, update rows whose content
hash changed, skip rows that are byte-identical to what's already stored
(no-op — never touches `updated_at` for an unchanged row).

Cross-resource FK resolution (e.g. jb2_order_line_items.jb2_order_id) is out
of scope here — that's P1-06/07's job once orders/routings/materials land;
this module only knows how to upsert one resource's own columns.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import Integer, select
from sqlalchemy.orm import Session

# findings §4: lastModDate is UTC, second-granularity, "...Z" suffix, no ms.
JB2_DATETIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_jb2_datetime(value: str) -> datetime:
    return datetime.strptime(value, JB2_DATETIME_FMT).replace(tzinfo=timezone.utc)


def format_jb2_datetime(value: datetime) -> str:
    return to_utc(value).strftime(JB2_DATETIME_FMT)


def to_utc(value: datetime) -> datetime:
    """Normalize to a UTC-aware datetime. SQLite round-trips datetime
    columns as naive (no real tz storage); Postgres returns them aware
    already. Every jb2/checkpoint datetime is UTC by contract (findings §4),
    so a naive value here is always meant as UTC. An aware value in any zone
    (Postgres returns timestamptz in the session tz) is CONVERTED to UTC, not
    just relabeled -- otherwise strftime('...Z') stamps Z on non-UTC wall-clock
    (G3-D1: emitted a 4h-off timeStart, phantom labor in JB2 costing)."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def content_hash(record: dict[str, Any]) -> str:
    """sha256 of the record's canonical (sorted-key) JSON form."""
    canonical = json.dumps(record, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def upsert_records(
    session: Session,
    model: type,
    records: list[dict[str, Any]],
    *,
    key_field: str,
    extract: Callable[[dict[str, Any]], dict[str, Any]],
    key_attr: str = "jb2_id",
    last_modified_field: str = "lastModDate",
    now: Callable[[], datetime] = utcnow,
) -> tuple[int, int]:
    """Upsert one page of raw JB2 records into a mirror table.

    ``key_field`` is the raw JB2 field carrying the record's unique id
    (e.g. ``uniqueID``); ``key_attr`` is the model column it maps to
    (default ``jb2_id``, matching every jb2_* mirror table). ``extract``
    maps a raw record to the model's own indexed columns (excluding the
    common mirror columns, which this function stamps itself).

    Returns ``(fetched, changed)`` — ``fetched`` is ``len(records)``,
    ``changed`` counts rows actually inserted or updated (hash differs).
    Identical rows are skipped entirely, including `updated_at`.
    """
    fetched = len(records)
    changed = 0
    sync_time = now()

    # Most mirrors key on the Text `jb2_id` column (str() is always right),
    # but a couple of masters (P1-08: jb2_reason_codes.reason_number) key on
    # their own natural Integer column instead -- cast to match so Postgres
    # doesn't choke on a str bound against an integer column.
    key_column = getattr(model, key_attr).property.columns[0]
    key_is_int = isinstance(key_column.type, Integer)

    for record in records:
        raw_key = record[key_field]
        key_value = int(raw_key) if key_is_int else str(raw_key)
        h = content_hash(record)
        raw_lm = record.get(last_modified_field)
        last_modified = parse_jb2_datetime(raw_lm) if raw_lm else None

        existing = session.scalars(
            select(model).where(getattr(model, key_attr) == key_value)
        ).one_or_none()

        if existing is not None and existing.content_hash == h:
            continue  # byte-identical payload -- no-op, don't touch updated_at

        changed += 1
        fields = extract(record)

        if existing is None:
            row = model(
                **{key_attr: key_value},
                payload=record,
                content_hash=h,
                jb2_last_modified=last_modified,
                synced_at=sync_time,
                **fields,
            )
            session.add(row)
        else:
            existing.payload = record
            existing.content_hash = h
            existing.jb2_last_modified = last_modified
            existing.synced_at = sync_time
            existing.updated_at = sync_time
            for col, value in fields.items():
                setattr(existing, col, value)

    return fetched, changed
