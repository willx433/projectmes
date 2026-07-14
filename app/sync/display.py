"""Display-only feed pollers (P1-13, DD §4.2 display row, CR-011).

`shopview/get-jobs` and `eci-aps/get-schedule` ignore every query param and
return their entire dataset unfiltered every call (18 MB / 6.5 MB, 30s+,
504s observed -- docs/jb2-api-findings.md §1, CR-011). They're a soft,
display-only dependency: polled at low cadence with a generous timeout
override, cached to `display_cache`, and never allowed to affect any other
sync resource -- a failure here only ever produces its own sync_runs row and
leaves the last-good cached payload in place.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.domain.models_jb2 import DisplayCache, SyncRun
from app.jb2.client import Jb2Client
from app.sync import runs

logger = logging.getLogger("app.sync.display")

# CR-011: the blanket 10s client timeout (app/jb2/client.py) will not survive
# either endpoint -- override per-call.
DISPLAY_TIMEOUT_S = 120.0
# CR-011: cadence >= 5 min.
DISPLAY_CADENCE_S = 300.0


@dataclass(frozen=True)
class DisplayResourceDef:
    name: str  # sync_runs resource name + display_cache key
    endpoint: str  # jb2 client path
    cadence_s: float = DISPLAY_CADENCE_S
    # eci-aps/get-schedule's {StartDateProject, EndDateProject, Data} envelope
    # isn't the usual {Data: [...]} shape (findings §1) -- cache it whole.
    unwrap: bool = True


DISPLAY_REGISTRY: list[DisplayResourceDef] = [
    DisplayResourceDef(name="jb2-shopview", endpoint="/shopview/get-jobs"),
    DisplayResourceDef(name="jb2-schedule", endpoint="/eci-aps/get-schedule", unwrap=False),
]


def run_display_cycle(
    session: Session,
    client: Jb2Client,
    resource_def: DisplayResourceDef,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SyncRun:
    """One poll of a display feed. Never raises -- isolated the same way
    `app.sync.worker.run_cycle` isolates a regular mirror resource: a
    failure or timeout here rolls back only this resource's own work and
    records onto its own sync_runs row, leaving the last-good display_cache
    payload untouched (never cleared on failure)."""
    started = now()
    error: str | None = None
    fetched = 0

    try:
        # take=1 is silently ignored server-side (CR-011) but still
        # satisfies the client's own unfiltered-GET guard cheaply.
        payload = client.get(
            resource_def.endpoint, take=1, timeout=DISPLAY_TIMEOUT_S, unwrap=resource_def.unwrap,
        )
        fetched = 1
        fetched_at = now()
        cache_row = session.get(DisplayCache, resource_def.name)
        if cache_row is None:
            session.add(DisplayCache(key=resource_def.name, payload=payload, fetched_at=fetched_at))
        else:
            cache_row.payload = payload
            cache_row.fetched_at = fetched_at
    except Exception as exc:  # noqa: BLE001 -- isolation is the point, same as run_cycle
        logger.exception("display_cycle_failed", extra={"sync_resource": resource_def.name})
        session.rollback()
        error = str(exc)
        fetched = 0

    finished = now()
    return runs.record_run(
        session,
        resource=resource_def.name,
        started_at=started,
        finished_at=finished,
        fetched=fetched,
        changed=0,
        error=error,
    )
