"""Outbox drainer (P1-09) — DD §4.5.

Drains `jb2_outbox` rows to JB2 with a strict FIFO guarantee **per
work_order_id** (rows with a NULL work_order_id share one FIFO stream). A
row that fails blocks every later row in its own stream (never skip ahead —
the ordering guarantee) but never blocks other streams.

Status lifecycle: pending -> sent -> confirmed | failed. JB2's write APIs
give no separate confirmation step today, so a successful POST here marks
`sent_at`/`confirmed_at` together and jumps straight to `confirmed` — the
`sent` state is a placeholder DD §4.5 leaves for a future verification read
(Phase 3) that would flip sent -> confirmed independently.

Permanent errors (4xx, `Jb2PermanentError`) park the row as `failed`
immediately — no retries, and the local record is never deleted (§4.5).
Transient errors (5xx / network / breaker-open) retry with exponential
backoff up to MAX_ATTEMPTS, then park as `failed` too, surfaced later on the
admin health page (P1-10) for manual `replay()`.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models_jb2 import JB2Outbox
from app.jb2.client import Jb2Client, Jb2Error, Jb2PermanentError

logger = logging.getLogger("app.outbox.drainer")

MAX_ATTEMPTS = 8
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 900.0  # 15 min ceiling — ponytail: no jitter, single drainer process


def _send_time_ticket(client: Jb2Client, row: JB2Outbox) -> None:
    """CR-018 (docs/jb2-api-findings.md §2 P0-R1): header + detail are
    created together in ONE nested `POST /time-tickets` -- `row.payload` is
    already the full body (`{employeeCode, ticketDate, allowClosedJobs,
    timeTicketDetails: [...]}`) built by `app.outbox.payloads.build_time_ticket`."""
    client.post("/time-tickets", row.payload)


def _send_time_ticket_detail_deprecated(client: Jb2Client, row: JB2Outbox) -> None:
    """Dead sender kept only so a stray pre-CR-018 `time_ticket_detail` row
    (enqueued before this rework shipped) parks with a clear message instead
    of "no sender registered". Real JB2 rejects a standalone detail POST
    against a separately-created header ("Cannot find Time Ticket...") -- see
    CR-018 / docs/jb2-api-findings.md §2 item 1. No new code should ever
    enqueue this kind; app.outbox.payloads only enqueues "time_ticket" now."""
    raise Jb2PermanentError(
        "outbox kind 'time_ticket_detail' is deprecated by CR-018 -- header+detail "
        "now ride one nested POST /time-tickets write (kind='time_ticket'); see "
        "docs/jb2-api-findings.md §2 and docs/CHANGE_REQUESTS.md CR-018"
    )


# kind -> sender(client, row). Left open for Phase 3 kinds (P3-10).
SenderFn = Callable[[Jb2Client, JB2Outbox], None]
DEFAULT_SENDERS: dict[str, SenderFn] = {
    "time_ticket": _send_time_ticket,
    "time_ticket_detail": _send_time_ticket_detail_deprecated,
}


def _backoff_delay(attempts: int) -> float:
    return min(BACKOFF_BASE_S * (2 ** (attempts - 1)), BACKOFF_CAP_S)


def _aware(dt: datetime | None) -> datetime | None:
    """Sqlite (test DB) round-trips DateTime columns as naive, dropping
    tzinfo; Postgres (prod) preserves it. Normalize to UTC-aware before
    comparing so drain_once works the same on both."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _pending_streams(session: Session) -> dict[uuid.UUID | None, list[JB2Outbox]]:
    """Rows still needing action (pending or parked), oldest first, grouped
    by work_order_id — the FIFO stream a row belongs to."""
    rows = (
        session.execute(
            select(JB2Outbox)
            .where(JB2Outbox.status.in_(("pending", "failed")))
            .order_by(JB2Outbox.created_at, JB2Outbox.id)
        )
        .scalars()
        .all()
    )
    streams: dict[uuid.UUID | None, list[JB2Outbox]] = defaultdict(list)
    for row in rows:
        streams[row.work_order_id].append(row)
    return streams


def _attempt(session: Session, client: Jb2Client, row: JB2Outbox,
             senders: dict[str, SenderFn], now: datetime) -> str:
    sender = senders.get(row.kind)
    row.attempts += 1
    log_extra = {
        "outbox_id": str(row.id), "outbox_kind": row.kind,
        "outbox_work_order_id": str(row.work_order_id) if row.work_order_id else None,
        "outbox_attempts": row.attempts,
    }
    try:
        if sender is None:
            raise Jb2PermanentError(f"no sender registered for outbox kind {row.kind!r}")
        sender(client, row)
    except Jb2PermanentError as exc:
        row.status = "failed"
        row.last_error = str(exc)[:2000]
        session.commit()
        logger.warning("outbox_park_permanent", extra={**log_extra, "error": row.last_error})
        return "failed"
    except Jb2Error as exc:
        row.last_error = str(exc)[:2000]
        if row.attempts >= MAX_ATTEMPTS:
            row.status = "failed"
            row.next_attempt_at = None
            session.commit()
            logger.warning("outbox_park_max_attempts", extra={**log_extra, "error": row.last_error})
            return "failed"
        row.next_attempt_at = now + timedelta(seconds=_backoff_delay(row.attempts))
        session.commit()
        logger.info("outbox_retry", extra={**log_extra, "error": row.last_error,
                                            "next_attempt_at": row.next_attempt_at.isoformat()})
        return "retry"
    else:
        row.status = "confirmed"
        row.sent_at = now
        row.confirmed_at = now
        row.next_attempt_at = None
        row.last_error = None
        session.commit()
        logger.info("outbox_confirmed", extra=log_extra)
        return "confirmed"


def drain_once(
    session: Session,
    client: Jb2Client,
    *,
    senders: dict[str, SenderFn] | None = None,
    now: datetime | None = None,
) -> list[tuple[uuid.UUID, str]]:
    """One pass over every FIFO stream. Per stream: attempt rows in created
    order, stop at the first row that isn't a clean 'confirmed' (already
    parked, not yet due for retry, or just failed/retried this pass) — that
    row and everything behind it in its stream stay untouched until it
    clears. Returns [(outbox_id, outcome), ...] for whatever was attempted.
    """
    senders = senders if senders is not None else DEFAULT_SENDERS
    now = now or datetime.now(timezone.utc)

    outcomes: list[tuple[uuid.UUID, str]] = []
    for _stream_key, rows in _pending_streams(session).items():
        for row in rows:
            if row.status == "failed":
                break  # parked row blocks the rest of this work order's stream
            if _aware(row.next_attempt_at) is not None and _aware(row.next_attempt_at) > now:
                break  # backoff not elapsed yet — still head of the stream
            outcome = _attempt(session, client, row, senders, now)
            outcomes.append((row.id, outcome))
            if outcome != "confirmed":
                break
    return outcomes


def replay(session: Session, outbox_id: uuid.UUID) -> None:
    """Manual replay (admin health page, P1-10): reset a parked row back to
    pending so the next drain pass picks it up again."""
    row = session.get(JB2Outbox, outbox_id)
    if row is None:
        raise ValueError(f"no outbox row {outbox_id}")
    if row.status != "failed":
        raise ValueError(f"outbox row {outbox_id} is not failed (status={row.status!r})")
    row.status = "pending"
    row.attempts = 0
    row.last_error = None
    row.next_attempt_at = None
    session.commit()


def run_forever(
    session_factory: Callable[[], Session],
    client: Jb2Client,
    *,
    senders: dict[str, SenderFn] | None = None,
    poll_interval_s: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
    stop_event: "object | None" = None,
) -> None:
    """The worker loop: drain, sleep, repeat, until stop_event is set.

    `stop_event` is anything with `.is_set()` (e.g. threading.Event) —
    ponytail: no daemon/signal-handling scaffolding here, that's a deploy
    concern (systemd unit restarts the process; P1-11).
    """
    while stop_event is None or not stop_event.is_set():
        session = session_factory()
        try:
            drain_once(session, client, senders=senders)
        finally:
            session.close()
        sleeper(poll_interval_s)
