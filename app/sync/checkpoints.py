"""Per-resource sync checkpoints (P1-05, DD §4.2).

A checkpoint is the last successfully processed `lastModDate` for a
resource, persisted in `sync_checkpoints` (migrations/versions/0002).
Polling uses `lastModDate[gte] = checkpoint - OVERLAP` so a restart or a
boundary-timestamp record is never silently missed — findings §4 already
found `gte` inclusive at the exact boundary (dedupe happens via
content_hash in engine.py, not by shrinking the window), but a small
overlap is kept anyway as insurance against any server-side timestamp
truncation not observed in the Phase 0 sample (findings §4's own
recommendation).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import config
from app.domain.models_jb2 import SyncCheckpoint
from app.sync.engine import to_utc

OVERLAP_S = 2  # findings §4: second-granularity, gte inclusive at boundary

# Sentinel floor for a resource's first-ever pull when it's exempt from
# BACKFILL_DAYS windowing (masters with supports_last_mod=False).
EPOCH_FLOOR = datetime(1970, 1, 1, tzinfo=timezone.utc)


def get_checkpoint(session: Session, resource: str) -> datetime | None:
    row = session.get(SyncCheckpoint, resource)
    if row is None or row.checkpoint is None:
        return None
    return to_utc(row.checkpoint)


def poll_since(session: Session, resource: str) -> datetime | None:
    """The `lastModDate[gte]` value to poll with, or None if this resource
    has never completed a cycle (caller picks the first-ever-pull floor)."""
    checkpoint = get_checkpoint(session, resource)
    if checkpoint is None:
        return None
    return checkpoint - timedelta(seconds=OVERLAP_S)


def advance_checkpoint(
    session: Session, resource: str, new_checkpoint: datetime, *, now: datetime
) -> None:
    """Advance the checkpoint forward only — never regress it."""
    row = session.get(SyncCheckpoint, resource)
    if row is None:
        session.add(SyncCheckpoint(resource=resource, checkpoint=new_checkpoint, updated_at=now))
        return
    if row.checkpoint is None or new_checkpoint > to_utc(row.checkpoint):
        row.checkpoint = new_checkpoint
        row.updated_at = now


def first_run_floor(
    session: Session, resource: str, *, supports_last_mod: bool, now: datetime
) -> datetime:
    """The `lastModDate[gte]` floor to use on a resource's first-ever cycle
    (no stored checkpoint yet). G1-D1: pulling all history unbounded fanned
    out ~3 child API calls per line item across 16,938 historical orders on
    the live tenant -- windowed to the last SYNC_BACKFILL_DAYS instead.

    Masters without lastModDate (small, full-pull resources) are exempt --
    floor stays the epoch, relying on upsert_records' hash diff to no-op
    anything already mirrored.

    Persisted immediately (own commit, independent of the caller's cycle)
    so a restart before the first cycle finishes doesn't recompute the
    window against a later `now` and skip records in between.
    """
    if not supports_last_mod:
        return EPOCH_FLOOR
    floor = now - timedelta(days=config.sync_backfill_days)
    advance_checkpoint(session, resource, floor, now=now)
    session.commit()
    return floor
