"""sync_runs recording (P1-05, DD §4.2).

Every poll cycle — success or failure — gets exactly one `sync_runs` row,
plus a structured log line (the "window" isn't a sync_runs column; DD §10's
schema is fixed, so it's logged instead, same pattern as
app/jb2/client.py's own per-call logging).
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.models_jb2 import SyncRun

logger = logging.getLogger("app.sync.runs")


def record_run(
    session: Session,
    *,
    resource: str,
    started_at: datetime,
    finished_at: datetime,
    fetched: int,
    changed: int,
    window_since: datetime | None = None,
    error: str | None = None,
) -> SyncRun:
    run = SyncRun(
        resource=resource,
        started_at=started_at,
        finished_at=finished_at,
        fetched=fetched,
        changed=changed,
        error=error,
    )
    session.add(run)

    logger.log(
        logging.ERROR if error else logging.INFO,
        "sync_run",
        extra={
            "sync_resource": resource,
            "sync_window_since": window_since.isoformat() if window_since else None,
            "sync_fetched": fetched,
            "sync_changed": changed,
            "sync_duration_ms": round((finished_at - started_at).total_seconds() * 1000, 1),
            "sync_error": error,
        },
    )
    return run
