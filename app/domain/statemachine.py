"""Execution state-machine core (P3-04). THE single server-side validator --
every floor endpoint mutates unit/session/scan state only through the
functions in this module (docs/state-machine.md preamble).

Governing contract: docs/state-machine.md (binding). Section refs below
(`S1`..`S10`, `O1`..`O8`, `§n`) point there. DD §6.3/§6.4/§6.7 are the prose
version of the same rules.

Scope of this task (P3-04): `POST /scan` resolution (the full S1-S10 table)
and the session open/pause/resume/close primitives it depends on.
Step/substep completion, finish-operation, kit-up, and failure/rework flows
are later tasks (P3-06/07/08/10/11) -- `unit_next_op`/`unit_op_done` are
written here as the shared position-pointer helpers those tasks will call,
but nothing in this module invokes them beyond scan resolution.

**Deviations from the literal contract (see final task report for detail):**
  - `scans.result` (migration 0007) has no `already_active` enum member --
    S10 (duplicate scan) is recorded as `result='accepted'` (it IS an
    accepted scan, just idempotent); the machine-readable `ScanResult.code`
    returned to the UI is still `already_active`.
  - Station-to-operation matching resolves the operation's work center via
    `plan_operation.jb2_routing_id -> jb2_order_routings.work_center_code`
    (frozen `plan_operations` rows don't carry work_center_code directly --
    only `operation_code`/`title`/frozen instruction content, per
    models_execution.py's freeze). A placeholder (`blocked=True`,
    `jb2_routing_id is None`) operation never station-matches, which is
    correct (it has no instructions yet -- a lead must resolve it first).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import service
from app.domain import events
from app.domain.events import _json_safe
from app.domain.models_execution import PlanOperation, Unit
from app.domain.models_floor import (
    PAUSE_REASON_CODES,
    WORK_SESSION_CLOSE_REASONS,
    BuildBox,
    Operator,
    RequestDedup,
    Scan,
    SessionPause,
    Station,
    Transit,
    WorkSession,
)
from app.domain.models_jb2 import JB2OrderRouting

# scans.result (migration 0007) has no code for every ScanResult.code --
# unmapped codes fall back to "rejected" (see module docstring's deviation note).
_SCAN_RESULT_FOR_CODE = {
    "unknown_box": "unknown_box",
    "unbound_box": "unbound_box",
    "wrong_station": "wrong_station",
    "accepted": "accepted",
    "already_active": "accepted",
}


@dataclass
class ScanResult:
    """Machine-readable scan outcome (contract §3/§10 codes) + a UI-rendering
    context dict. `to_dict()` is what the endpoint returns as JSON."""

    code: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "context": _json_safe(self.context)}


# -- §7 idempotency ----------------------------------------------------------


def with_request_dedup(
    session: Session,
    request_id: str | None,
    endpoint: str,
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Replays `endpoint`+`request_id` by returning the first response
    verbatim instead of re-running `fn` (contract §7/§17.9). No-ops (always
    runs `fn`) when `request_id` is None -- not every caller opts in."""
    if request_id is None:
        return fn()

    existing = session.execute(
        select(RequestDedup).where(
            RequestDedup.request_id == request_id, RequestDedup.endpoint == endpoint
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.response

    result = fn()
    session.add(RequestDedup(request_id=request_id, endpoint=endpoint, response=result))
    session.flush()
    return result


# -- §7 row lock --------------------------------------------------------------


def lock_unit(session: Session, unit_id: uuid.UUID) -> Unit | None:
    """`SELECT ... FOR UPDATE` on Postgres so concurrent scans for the same
    unit serialize. On SQLite (unit tests) `with_for_update()` is a no-op --
    the tests rely on single-writer access, matching contract §7's note."""
    stmt = select(Unit).where(Unit.id == unit_id)
    if session.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


# -- §2 per-unit operation position pointer -----------------------------------


def unit_next_op(session: Session, unit: Unit) -> PlanOperation | None:
    """The unit's next pending PlanOperation (contract §2:
    `units.current_plan_op_id` is the position pointer). Lazily resolves +
    persists the pointer to the work order's first non-skipped op if unset."""
    if unit.current_plan_op_id is not None:
        op = session.get(PlanOperation, unit.current_plan_op_id)
        if op is not None:
            return op

    op = session.scalars(
        select(PlanOperation)
        .where(
            PlanOperation.work_order_id == unit.work_order_id,
            PlanOperation.status != "skipped",
        )
        .order_by(PlanOperation.seq)
    ).first()
    if op is not None:
        unit.current_plan_op_id = op.id
    return op


def unit_op_done(session: Session, unit: Unit, plan_op: PlanOperation) -> PlanOperation | None:
    """Advances `unit`'s position pointer past `plan_op` to the next
    non-skipped op (or None at the end of the route). Written for the
    finish-operation flow (P3-10) -- not called by scan resolution itself."""
    next_op = session.scalars(
        select(PlanOperation)
        .where(
            PlanOperation.work_order_id == unit.work_order_id,
            PlanOperation.seq > plan_op.seq,
            PlanOperation.status != "skipped",
        )
        .order_by(PlanOperation.seq)
    ).first()
    unit.current_plan_op_id = next_op.id if next_op else None
    return next_op


def _op_work_center(session: Session, plan_op: PlanOperation | None) -> str | None:
    if plan_op is None or plan_op.jb2_routing_id is None:
        return None
    routing = session.get(JB2OrderRouting, plan_op.jb2_routing_id)
    return routing.work_center_code if routing else None


def _station_matches(session: Session, station: Station, plan_op: PlanOperation) -> bool:
    wc = _op_work_center(session, plan_op)
    return (
        wc is not None and station.work_center_code is not None and wc == station.work_center_code
    )


def _op_for_station(
    session: Session, station: Station, work_order_id: uuid.UUID
) -> PlanOperation | None:
    """O2 override target: the work order's operation whose work center
    matches this station (earliest by seq), regardless of the unit's actual
    position pointer -- "session opens against the out-of-seq op"."""
    if station.work_center_code is None:
        return None
    ops = session.scalars(
        select(PlanOperation)
        .where(PlanOperation.work_order_id == work_order_id)
        .order_by(PlanOperation.seq)
    ).all()
    for op in ops:
        if _op_work_center(session, op) == station.work_center_code:
            return op
    return None


# -- scans/events plumbing ------------------------------------------------------


def _record_scan(
    session: Session,
    *,
    box_id: uuid.UUID | None,
    station: Station,
    operator: Operator | None,
    raw_payload: str,
    result: str,
    override_by: uuid.UUID | None = None,
    work_session_id: uuid.UUID | None = None,
) -> Scan:
    scan = Scan(
        box_id=box_id,
        station_id=station.id,
        operator_id=operator.id if operator else None,
        raw_payload=raw_payload,
        result=result,
        override_by=override_by,
        work_session_id=work_session_id,
        # app-level UTC (not the DB server_default): matches every other
        # timestamp in the system and keeps microsecond ordering, so same-second
        # scans don't misorder in the audit trail (SQLite server_default is
        # whole-second). Same class as G3-D1.
        scanned_at=datetime.now(timezone.utc),
    )
    session.add(scan)
    session.flush()
    return scan


def _reject(
    session: Session,
    station: Station,
    operator: Operator | None,
    raw_payload: str,
    code: str,
    *,
    box_id: uuid.UUID | None = None,
    unit: Unit | None = None,
    context: dict[str, Any] | None = None,
) -> ScanResult:
    scan_result = _SCAN_RESULT_FOR_CODE.get(code, "rejected")
    scan = _record_scan(
        session, box_id=box_id, station=station, operator=operator, raw_payload=raw_payload,
        result=scan_result,
    )
    ctx = dict(context or {})
    if unit is not None:
        ctx["unit_id"] = str(unit.id)
        ctx["unit_status"] = unit.status
    events.emit(
        session, "scan.rejected", entity=scan, actor_id=operator.id if operator else None,
        station_id=station.id, after={"code": code, **ctx},
    )
    return ScanResult(code, ctx)


def _scan_context(
    unit: Unit, plan_op: PlanOperation, work_session: WorkSession, station: Station
) -> dict[str, Any]:
    return {
        "unit_id": str(unit.id),
        "work_order_id": str(unit.work_order_id),
        "plan_operation_id": str(plan_op.id),
        "operation_title": plan_op.title,
        "station_id": str(station.id),
        "work_session_id": str(work_session.id),
        "unit_status": unit.status,
    }


def _busy_elsewhere(
    session: Session, unit: Unit, operator: Operator, station: Station
) -> WorkSession | None:
    """S9: another operator's open session on this unit at a different
    station. Two operators at the SAME station is allowed (§17.7)."""
    return session.execute(
        select(WorkSession).where(
            WorkSession.unit_id == unit.id,
            WorkSession.ended_at.is_(None),
            WorkSession.station_id != station.id,
            WorkSession.operator_id != operator.id,
        )
    ).scalars().first()


def _existing_open_session(
    session: Session, unit: Unit, plan_op: PlanOperation, operator: Operator, station: Station
) -> WorkSession | None:
    """S10: this exact (unit, op, operator, station) already has an open
    session -- a re-scan is idempotent, not a second session."""
    return session.execute(
        select(WorkSession).where(
            WorkSession.unit_id == unit.id,
            WorkSession.plan_operation_id == plan_op.id,
            WorkSession.operator_id == operator.id,
            WorkSession.station_id == station.id,
            WorkSession.ended_at.is_(None),
        )
    ).scalars().first()


def _close_open_transit(session: Session, unit: Unit, station: Station, now: datetime) -> None:
    transit = session.execute(
        select(Transit)
        .where(Transit.unit_id == unit.id, Transit.arrived_at.is_(None))
        .order_by(Transit.departed_at.desc())
    ).scalars().first()
    if transit is not None:
        transit.arrived_at = now
        transit.to_station_id = transit.to_station_id or station.id
        # ponytail: sqlite round-trips DateTime columns as naive even when a
        # tz-aware value was written -- normalize both sides to naive UTC for
        # the subtraction rather than assuming the reloaded row kept tzinfo.
        departed = transit.departed_at
        if departed.tzinfo is not None:
            departed = departed.astimezone(timezone.utc).replace(tzinfo=None)
        now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
        transit.seconds = int((now_naive - departed).total_seconds())


def _session_kind(session: Session, unit: Unit, plan_op: PlanOperation, setup: bool) -> str:
    """Contract §6: kind is first_pass/rework by `unit.first_pass`, unless
    `setup` is requested AND this is the first WorkSession ever opened for
    (unit, plan_op)."""
    if setup:
        prior = session.execute(
            select(WorkSession).where(
                WorkSession.unit_id == unit.id, WorkSession.plan_operation_id == plan_op.id
            )
        ).scalars().first()
        if prior is None:
            return "setup"
    return "first_pass" if unit.first_pass else "rework"


# -- badge (S1/S2) -------------------------------------------------------------


def _resolve_badge(session: Session, payload: str, station: Station) -> ScanResult:
    """S1/S2: delegates entirely to the auth service's login logic. Cookie
    handling is the endpoint's job, not this module's."""
    try:
        operator = service.badge_login(session, station, payload=payload)
    except service.AuthError:
        return ScanResult("badge_rejected", {})
    return ScanResult(
        "operator_session_opened",
        {
            "operator_id": str(operator.id),
            "display_name": operator.display_name,
            "roles": operator.roles,
            "station_id": str(station.id),
        },
    )


# -- box accept (S6/O2) --------------------------------------------------------


def _accept(
    session: Session,
    *,
    box: BuildBox,
    unit: Unit,
    plan_op: PlanOperation,
    station: Station,
    operator: Operator,
    raw_payload: str,
    setup: bool,
    override_by: Operator | None,
    now: datetime,
) -> ScanResult:
    if unit.status == "in_transit":
        _close_open_transit(session, unit, station, now)
    unit.status = "at_station"

    kind = _session_kind(session, unit, plan_op, setup)
    work_session = open_session(
        session, unit=unit, operator=operator, station=station, plan_op=plan_op, kind=kind, now=now,
    )

    scan = _record_scan(
        session, box_id=box.id, station=station, operator=operator, raw_payload=raw_payload,
        result="accepted", work_session_id=work_session.id,
        override_by=override_by.id if override_by else None,
    )
    events.emit(
        session, "scan.accepted", entity=scan, actor_id=operator.id, station_id=station.id,
        after={
            "unit_id": str(unit.id), "plan_operation_id": str(plan_op.id),
            "work_session_id": str(work_session.id), "override": override_by is not None,
        },
    )
    if override_by is not None:
        events.emit(
            session, "auth.override", entity=unit, actor_id=override_by.id, station_id=station.id,
            after={"action": "wrong_station_accept", "acting_operator_id": str(operator.id)},
        )
    return ScanResult("accepted", _scan_context(unit, plan_op, work_session, station))


# -- the box branch of the S1-S10 table ----------------------------------------


def _resolve_box(
    session: Session,
    payload: str,
    station: Station,
    operator: Operator | None,
    *,
    override_by: Operator | None,
    setup: bool,
) -> ScanResult:
    now = datetime.now(timezone.utc)

    if operator is None:  # S3
        return _reject(session, station, operator, payload, "operator_required")

    box = session.execute(
        select(BuildBox).where(BuildBox.qr_payload == payload)
    ).scalar_one_or_none()
    if box is None:  # S4
        return _reject(session, station, operator, payload, "unknown_box")

    if box.current_unit_id is None:  # S5
        return _reject(session, station, operator, payload, "unbound_box", box_id=box.id)

    unit = lock_unit(session, box.current_unit_id)
    if unit is None:  # defensive -- dangling current_unit_id
        return _reject(session, station, operator, payload, "unbound_box", box_id=box.id)

    if unit.status in ("done", "scrapped"):  # S8
        return _reject(
            session, station, operator, payload, "unit_terminal", box_id=box.id, unit=unit
        )

    busy = _busy_elsewhere(session, unit, operator, station)
    if busy is not None:  # S9
        return _reject(
            session, station, operator, payload, "unit_busy_elsewhere", box_id=box.id, unit=unit,
            context={
                "busy_operator_id": str(busy.operator_id), "busy_station_id": str(busy.station_id),
            },
        )

    plan_op = unit_next_op(session, unit)
    if plan_op is None:  # every op done/skipped but unit not yet flipped to done -- terminal-ish
        return _reject(
            session, station, operator, payload, "unit_terminal", box_id=box.id, unit=unit
        )

    if _station_matches(session, station, plan_op):  # S6 / S10
        existing = _existing_open_session(session, unit, plan_op, operator, station)
        if existing is not None:
            scan = _record_scan(
                session, box_id=box.id, station=station, operator=operator, raw_payload=payload,
                result="accepted", work_session_id=existing.id,
            )
            events.emit(
                session, "scan.accepted", entity=scan, actor_id=operator.id, station_id=station.id,
                after={"idempotent": True, "work_session_id": str(existing.id)},
            )
            return ScanResult("already_active", _scan_context(unit, plan_op, existing, station))

        return _accept(
            session, box=box, unit=unit, plan_op=plan_op, station=station, operator=operator,
            raw_payload=payload, setup=setup, override_by=None, now=now,
        )

    # wrong station (S7), unless a lead override (O2) names an operation this
    # station actually performs
    if override_by is not None:
        target_op = _op_for_station(session, station, unit.work_order_id)
        if target_op is not None:
            return _accept(
                session, box=box, unit=unit, plan_op=target_op, station=station, operator=operator,
                raw_payload=payload, setup=setup, override_by=override_by, now=now,
            )

    return _reject(
        session, station, operator, payload, "wrong_station", box_id=box.id, unit=unit,
        context={
            "expected_operation": plan_op.title,
            "expected_work_center": _op_work_center(session, plan_op),
        },
    )


# -- public entry point ---------------------------------------------------------


def resolve_scan(
    session: Session,
    payload: str,
    station: Station,
    operator: Operator | None = None,
    *,
    override_by: Operator | None = None,
    setup: bool = False,
    request_id: str | None = None,
) -> ScanResult:
    """POST /scan resolver -- the full S1-S10 table. Validates, mutates, and
    emits events in `session` (no commit -- caller's transaction). Wraps
    itself in `with_request_dedup` when `request_id` is given; S10's
    duplicate-scan idempotency is independent of that and always active."""

    def _run() -> dict[str, Any]:
        if payload.startswith("OP:"):
            result = _resolve_badge(session, payload, station)
        else:
            result = _resolve_box(
                session, payload, station, operator, override_by=override_by, setup=setup
            )
        return result.to_dict()

    cached = with_request_dedup(session, request_id, "POST /scan", _run)
    return ScanResult(cached["code"], cached.get("context", {}))


# -- §6 sessions ------------------------------------------------------------


def open_session(
    session: Session,
    *,
    unit: Unit,
    operator: Operator,
    station: Station,
    plan_op: PlanOperation,
    kind: str,
    now: datetime | None = None,
) -> WorkSession:
    now = now or datetime.now(timezone.utc)
    work_session = WorkSession(
        unit_id=unit.id, plan_operation_id=plan_op.id, operator_id=operator.id,
        station_id=station.id, started_at=now, kind=kind,
    )
    session.add(work_session)
    session.flush()
    events.emit(
        session, "session.opened", entity=work_session, actor_id=operator.id, station_id=station.id,
        after={"unit_id": str(unit.id), "plan_operation_id": str(plan_op.id), "kind": kind},
    )
    return work_session


def pause_session(
    session: Session,
    work_session: WorkSession,
    *,
    reason_code: str,
    actor_id: uuid.UUID | None = None,
) -> SessionPause:
    if reason_code not in PAUSE_REASON_CODES:
        raise ValueError(f"unknown pause reason_code: {reason_code!r}")
    if work_session.ended_at is not None:
        raise ValueError("cannot pause a closed session")

    open_pause = session.execute(
        select(SessionPause).where(
            SessionPause.work_session_id == work_session.id, SessionPause.ended_at.is_(None)
        )
    ).scalar_one_or_none()
    if open_pause is not None:
        return open_pause  # ponytail: idempotent -- already paused, no-op

    pause = SessionPause(work_session_id=work_session.id, reason_code=reason_code)
    session.add(pause)
    session.flush()
    events.emit(
        session, "session.paused", entity=work_session, actor_id=actor_id,
        station_id=work_session.station_id, after={"reason_code": reason_code},
    )
    return pause


def resume_session(
    session: Session, work_session: WorkSession, *, actor_id: uuid.UUID | None = None
) -> WorkSession:
    if work_session.ended_at is not None:
        raise ValueError("cannot resume a closed session")

    open_pause = session.execute(
        select(SessionPause).where(
            SessionPause.work_session_id == work_session.id, SessionPause.ended_at.is_(None)
        )
    ).scalar_one_or_none()
    if open_pause is None:
        return work_session  # ponytail: idempotent -- not paused, no-op

    open_pause.ended_at = datetime.now(timezone.utc)
    events.emit(
        session, "session.resumed", entity=work_session, actor_id=actor_id,
        station_id=work_session.station_id, after={"pause_id": str(open_pause.id)},
    )
    return work_session


def close_session(
    session: Session,
    work_session: WorkSession,
    *,
    reason: str,
    actor_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> WorkSession:
    """§6/§17.8: `reason` in ('finished','clocked_out','auto_closed').
    `clocked_out` partial-time semantics are just "close now, unit stays
    at_station" -- there is no separate unit mutation here (finish's
    in_transit/done transition is a different, later function -- P3-10).
    Double-close is a no-op (§17.9 finish idempotency)."""
    if reason not in WORK_SESSION_CLOSE_REASONS:
        raise ValueError(f"unknown close reason: {reason!r}")
    if work_session.ended_at is not None:
        return work_session

    now = now or datetime.now(timezone.utc)
    open_pause = session.execute(
        select(SessionPause).where(
            SessionPause.work_session_id == work_session.id, SessionPause.ended_at.is_(None)
        )
    ).scalar_one_or_none()
    if open_pause is not None:
        open_pause.ended_at = now

    work_session.ended_at = now
    work_session.close_reason = reason
    events.emit(
        session, "session.closed", entity=work_session, actor_id=actor_id,
        station_id=work_session.station_id, after={"reason": reason},
    )
    return work_session


def auto_close_idle(
    session: Session, now: datetime | None = None, *, idle_paused_minutes: int = 60
) -> list[WorkSession]:
    """§6/§17.8: sessions paused longer than `idle_paused_minutes` auto-close
    (`close_reason='auto_closed'`, `lead_confirmed` stays False -- no outbox
    post until O6). Pure query+transition function; a worker (P3-08/09)
    calls this on a timer -- no scheduler lives here."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=idle_paused_minutes)
    stale_pauses = session.execute(
        select(SessionPause).where(
            SessionPause.ended_at.is_(None), SessionPause.started_at <= cutoff
        )
    ).scalars().all()

    closed = []
    for pause in stale_pauses:
        work_session = session.get(WorkSession, pause.work_session_id)
        if work_session is not None and work_session.ended_at is None:
            closed.append(close_session(session, work_session, reason="auto_closed", now=now))
    return closed
