"""Session lifecycle plumbing beyond the state-machine primitives (P3-08/09,
DD §6.7/§17.8, docs/state-machine.md §6/§4 O6).

Builds on `app.domain.statemachine`'s `pause_session`/`resume_session`/
`close_session`/`auto_close_idle` (never duplicates their logic) to add the
two pieces that don't fit that module's own scope: the idle-then-auto-close
sweep that needs a worker loop (`run_idle_sweep`, driven by
`python -m app.floorworker`), and the O6 lead-confirm gate that turns an
`auto_closed` session's withheld time into an actual outbox write
(`lead_confirm_session`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import config
from app.domain import events, statemachine
from app.domain.models_execution import PlanOperation, Unit
from app.domain.models_floor import (
    Operator,
    SessionPause,
    StepExecution,
    SubstepExecution,
    WorkSession,
)
from app.outbox import payloads

# ponytail: PAUSE_REASON_CODES (models_floor.py) has no dedicated "idle"/
# "system" entry -- the DD's literal enum is waiting_material, machine_down,
# break, pulled_to_other_job, other. "other" is the closest fit for an
# automatic idle-pause; loud comment rather than silently picking one.
IDLE_AUTO_PAUSE_REASON = "other"


class LeadRequiredError(ValueError):
    """Raised when lead_confirm_session is attempted by a non-lead operator."""


def _naive_utc(dt: datetime) -> datetime:
    # ponytail: same sqlite-naive/postgres-aware normalization trick used in
    # app/domain/statemachine.py (_close_open_transit) and app/outbox/drainer.py.
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _last_activity_at(session: Session, work_session: WorkSession) -> datetime:
    """Best-effort "still being worked" signal for `work_session`: the
    latest of the session's own start and any substep its operator
    completed on this (unit, plan_operation) since. No dedicated
    "session activity" column exists (substep_executions belongs to
    P3-06/07) -- this reads what's already there rather than adding one."""
    step_exec_ids = session.scalars(
        select(StepExecution.id).where(
            StepExecution.unit_id == work_session.unit_id,
            StepExecution.plan_operation_id == work_session.plan_operation_id,
        )
    ).all()
    latest = None
    if step_exec_ids:
        latest = session.execute(
            select(func.max(SubstepExecution.completed_at)).where(
                SubstepExecution.step_execution_id.in_(step_exec_ids),
                SubstepExecution.operator_id == work_session.operator_id,
            )
        ).scalar()
    candidates = [t for t in (work_session.started_at, latest) if t is not None]
    return max(_naive_utc(t) for t in candidates)


def run_idle_sweep(
    session_factory: Callable[[], Session], now: datetime | None = None,
) -> list[WorkSession]:
    """Worker-loop body (`app/floorworker/__main__.py`, systemd `mes-floor`):

    1. Auto-pause any open, not-already-paused `WorkSession` idle for
       `STATION_SESSION_IDLE_MIN` (no substep activity) -- reason `other`.
    2. Delegate to `statemachine.auto_close_idle` for the existing
       paused-longer-than-`SESSION_AUTO_CLOSE_MIN` -> `auto_closed`
       transition (no outbox post -- withheld until `lead_confirm_session`).

    Returns the sessions auto-closed this pass (mirrors `auto_close_idle`'s
    own return so callers/tests can assert on it directly). Opens and
    commits its own session(s) via `session_factory` -- this is a worker
    entry point, not a per-request dependency.
    """
    now = now or datetime.now(timezone.utc)
    idle_cutoff = _naive_utc(now) - timedelta(minutes=max(config.station_session_idle_min, 0))

    db = session_factory()
    try:
        open_sessions = db.scalars(select(WorkSession).where(WorkSession.ended_at.is_(None))).all()
        for ws in open_sessions:
            open_pause = db.execute(
                select(SessionPause).where(
                    SessionPause.work_session_id == ws.id, SessionPause.ended_at.is_(None)
                )
            ).scalar_one_or_none()
            if open_pause is not None:
                continue  # already paused -- auto_close_idle (below) owns it from here

            if _last_activity_at(db, ws) <= idle_cutoff:
                statemachine.pause_session(db, ws, reason_code=IDLE_AUTO_PAUSE_REASON)
        db.commit()

        closed = statemachine.auto_close_idle(
            db, now, idle_paused_minutes=max(config.session_auto_close_min, 0)
        )
        db.commit()
        return closed
    finally:
        db.close()


def lead_confirm_session(
    session: Session, work_session: WorkSession, lead_operator: Operator,
) -> WorkSession:
    """O6 (state-machine.md §4): confirming an `auto_closed` session is the
    ONLY moment its time posts to JB2 -- the outbox enqueue happens HERE,
    never at auto-close itself (§17.8: keep idle/garbage time out of
    costing until a human vouches for it). Idempotent: confirming an
    already-confirmed session is a no-op (no double enqueue -- the
    downstream idempotency key would no-op it anyway, but this avoids the
    redundant query/log)."""
    if "lead" not in (lead_operator.roles or []):
        raise LeadRequiredError("lead role required to confirm a session")
    if work_session.close_reason != "auto_closed":
        raise ValueError("only an auto_closed session requires lead confirmation")
    if work_session.lead_confirmed:
        return work_session

    work_session.lead_confirmed = True
    events.emit(
        session, "session.confirmed", entity=work_session, actor_id=lead_operator.id,
        station_id=work_session.station_id, after={"work_session_id": str(work_session.id)},
    )

    unit = session.get(Unit, work_session.unit_id)
    plan_op = session.get(PlanOperation, work_session.plan_operation_id)
    # An auto_closed session's time was idle/withheld, not a completed op --
    # pieces_finished=0 is the correct default (a real finish already went
    # through app/api/operations.py's own enqueue, not this path).
    payloads.enqueue_finish_writeback(session, work_session, unit, plan_op, pieces_finished=0)
    return work_session
