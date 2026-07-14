"""Outbox writer (P1-09) — DD §4.5.

`enqueue()` inserts a `jb2_outbox` row in the **caller's** session/transaction:
no commit happens here, so the outbox row and the local domain event it rides
with either both land or both roll back together (the same-transaction
guarantee the design doc requires). The caller commits.

Idempotency keys are the caller's job to make deterministic (`wo:{id}:op:
{seq}:finish`); `make_key()` is a small formatting helper for that. Enqueuing
the same key twice is a no-op that returns the existing row's id — callers
don't need to pre-check for a duplicate themselves.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models_jb2 import JB2Outbox


def make_key(*parts: Any) -> str:
    """Join parts into a deterministic idempotency key, e.g.
    ``make_key("wo", wo_id, "op", seq, "finish") == "wo:<id>:op:<seq>:finish"``.
    """
    return ":".join(str(p) for p in parts)


def enqueue(
    session: Session,
    kind: str,
    payload: dict,
    idempotency_key: str,
    work_order_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert a pending jb2_outbox row in `session`'s current transaction.

    Returns the row id — either the newly inserted one, or the id of the
    row that already held this idempotency_key (idempotent enqueue, never
    raises on a duplicate).

    ponytail: duplicate detection is a plain SELECT-then-INSERT, not a
    SAVEPOINT around the unique-constraint race — the outbox has a single
    writer per work order in practice (one WorkSession finishing an op).
    Add a nested savepoint here if concurrent enqueuers for the same key
    ever show up.
    """
    existing = session.execute(
        select(JB2Outbox.id).where(JB2Outbox.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = JB2Outbox(
        id=uuid.uuid4(),
        kind=kind,
        payload=payload,
        idempotency_key=idempotency_key,
        work_order_id=work_order_id,
        # Set explicitly (not left to the DB server_default) so FIFO
        # ordering has microsecond resolution — Postgres's own now() would
        # be precise enough, but sqlite's CURRENT_TIMESTAMP is only
        # second-granular and would tie-break same-second rows on id.
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row.id
