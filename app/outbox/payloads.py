"""JB2 write-back payload builders (P3-10, DD §4.4 as amended by CR-010 and
reworked by CR-018 against live-verified findings, docs/jb2-api-findings.md
§2 P0-R1). Time-ticket writes are the **sole** JB2 write-back for operation
completion/scrap -- there is no routing-step PATCH (CR-010: `OrderRoutingUpdate`
is `additionalProperties:false` and doesn't carry actuals/status at all).

**CR-018 rework:** live sandbox writes proved the header+detail two-call
model (separate `ensure_time_ticket_header` + `time_ticket_detail` POST)
wrong -- a standalone `POST /time-ticket-details` against a separately
created header 400s ("Cannot find Time Ticket..."). The only write that
works is ONE nested `POST /time-tickets` carrying `timeTicketDetails: [{...}]`.
So there is one outbox row (`kind="time_ticket"`) per closed work session,
built by `build_time_ticket` below, and `app/outbox/drainer.py` has a single
`time_ticket` sender that POSTs the whole nested body.

Three call sites enqueue through `enqueue_finish_writeback` (the one public
entry point): `app/api/operations.py`'s finish endpoint (pieces_finished=1),
`app/domain/failures.py`'s scrap path (pieces_scrapped=1), and
`app/domain/sessions.py`'s O6 `lead_confirm_session` (an auto_closed
session's withheld time, pieces_finished=0).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import events
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import Operator, SessionPause, WorkSession
from app.domain.models_jb2 import (
    JB2Employee,
    JB2OrderLineItem,
    MappingException,
)
from app.outbox import writer
from app.sync.engine import format_jb2_datetime

logger = logging.getLogger("app.outbox.payloads")


def _naive_utc(dt: datetime) -> datetime:
    # ponytail: sqlite round-trips DateTime columns naive even when an
    # aware value was written; Postgres preserves tzinfo. Normalize both
    # sides to naive-UTC before arithmetic, same trick statemachine.py and
    # outbox/drainer.py already use.
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _record_mapping_exception(session: Session, kind: str, value: str, context: dict) -> None:
    already = session.execute(
        select(MappingException).where(
            MappingException.kind == kind, MappingException.value == value,
            MappingException.resolved.is_(False),
        )
    ).first()
    if already is not None:
        return  # ponytail: don't spam a new row every finish/scrap/confirm for the same gap
    session.add(MappingException(kind=kind, value=value, context=context, resolved=False))


def _employee_code_int(session: Session, operator: Operator) -> int | None:
    if operator.jb2_employee_id is None:
        return None
    jb2_emp = session.get(JB2Employee, operator.jb2_employee_id)
    if jb2_emp is None or not jb2_emp.employee_code:
        return None
    try:
        return int(jb2_emp.employee_code)
    except (TypeError, ValueError):
        return None


def _job_number(session: Session, work_order: WorkOrder | None) -> str | None:
    if work_order is None:
        return None
    line_item = session.get(JB2OrderLineItem, work_order.jb2_line_item_id)
    if line_item is None:
        return None
    # `jobNumber` (e.g. "10008-01") is never promoted to its own mirror
    # column (app/domain/models_jb2.py's JB2OrderLineItem has no such
    # field -- it's only used transiently during sync to fan out
    # routing/materials fetches) -- read it back off the raw payload every
    # mirror row carries (MirrorMixin.payload), the same source sync itself
    # reads job_number from (see app/sync/worker.py's `_sync_line_item_children`).
    return (line_item.payload or {}).get("jobNumber")


def _elapsed_hours(session: Session, work_session: WorkSession) -> float:
    """Session time = closed_at - opened_at - sum(pauses) (contract §6)."""
    end = work_session.ended_at or datetime.now(timezone.utc)
    total_s = (_naive_utc(end) - _naive_utc(work_session.started_at)).total_seconds()
    pauses = session.scalars(
        select(SessionPause).where(
            SessionPause.work_session_id == work_session.id, SessionPause.ended_at.is_not(None)
        )
    ).all()
    for p in pauses:
        total_s -= (_naive_utc(p.ended_at) - _naive_utc(p.started_at)).total_seconds()
    return max(total_s, 0.0) / 3600.0


def _ticket_date(work_session: WorkSession) -> str:
    d = _naive_utc(work_session.started_at).date()
    return format_jb2_datetime(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))


def _hhmm(dt: datetime) -> str:
    """`HH:MM` clock string (max length 5 -- findings §2 item 2; sending a
    full ISO datetime fails `400 "value for field timeStart exceeds maximum
    length of 5"`).

    TODO(site-tz): renders the naive-UTC wall clock -- this codebase has no
    per-site timezone setting yet. Correct only while the shop floor and the
    JB2 tenant's server-local day agree with UTC (findings §2 item 4:
    `ticketDate` is normalized to the server's local midnight); add a real
    site-tz conversion here if a shift ever straddles a UTC day boundary in
    practice.
    """
    return _naive_utc(dt).strftime("%H:%M")


def _time_fields(session: Session, work_session: WorkSession) -> dict:
    """**RESOLVED** (docs/jb2-api-findings.md §2 P0-R1, items 2-3 -- live
    sandbox writes, no longer BLOCKED-ON-WRITE-ACCESS/pending): `timeStart`/
    `timeEnd` and `setupTime` are COMPLEMENTS, not alternates -- JB2 DERIVES
    `cycleTime` itself from `timeStart`/`timeEnd` (a 14:38->14:43 detail read
    back `cycleTime: 0.083`). So: `kind='setup'` sessions send `setupTime`
    (elapsed decimal hours) and omit timeStart/timeEnd entirely; every other
    session kind sends `timeStart`/`timeEnd` as `HH:MM` clock strings and
    never sends `cycleTime`/`setupTime` (JB2 computes it). This function
    stays the one place the branch lives.
    """
    if work_session.kind == "setup":
        return {"setupTime": _elapsed_hours(session, work_session)}
    fields = {"timeStart": _hhmm(work_session.started_at)}
    if work_session.ended_at is not None:
        fields["timeEnd"] = _hhmm(work_session.ended_at)
    return fields


def build_time_ticket(
    session: Session,
    work_session: WorkSession,
    unit: Unit,
    plan_op: PlanOperation,
    *,
    pieces_finished: int = 0,
    pieces_scrapped: int = 0,
    reason_number: int | None = None,
) -> dict | None:
    """Builds the single nested `TimeTicketCreate` body -- header fields
    plus one `timeTicketDetails[]` entry -- for one closed `work_session`
    (CR-018, docs/jb2-api-findings.md §2 P0-R1: header + detail MUST be
    created together in one `POST /time-tickets`; a standalone detail POST
    against a separately-created header 400s "Cannot find Time Ticket...").

    Omits `operationNumber` (findings §2 item 5: distinct numeric op id,
    != `stepNumber`, sending `stepNumber` as `operationNumber` 400s) and
    `workCenter` (item 6: a numeric work-center id, not the string code used
    elsewhere -- nullable on the JB2 side, so just leaving it out is a safe
    default; TODO: build a work-center code->numeric-id map and resolve it
    here once that mapping exists). Sets `allowClosedJobs: true` on the
    header (item 7) so a late write after a job closes doesn't 400.

    Returns `None` (never raises) when the operator has no linked JB2
    employee with a usable numeric `employeeCode`, or the work order's line
    item has no resolvable `jobNumber` -- both required fields on the JB2
    side. Either gap records a `mapping_exceptions` row + a loud warning
    log instead of enqueueing garbage (DD §4.6: unmapped values are an
    admin to-do, never a crash)."""
    operator = session.get(Operator, work_session.operator_id)
    employee_code = _employee_code_int(session, operator) if operator is not None else None
    if employee_code is None:
        _record_mapping_exception(
            session, "jb2_employee",
            str(operator.id if operator is not None else work_session.operator_id),
            {
                "reason": "operator has no linked jb2_employee with a numeric employee_code",
                "work_session_id": str(work_session.id),
            },
        )
        logger.warning(
            "outbox_skipped_no_employee_mapping",
            extra={
                "work_session_id": str(work_session.id),
                "operator_id": str(work_session.operator_id),
            },
        )
        return None

    work_order = session.get(WorkOrder, unit.work_order_id)
    job_number = _job_number(session, work_order)
    if job_number is None:
        _record_mapping_exception(
            session, "jb2_job_number", str(unit.work_order_id),
            {"reason": "work order's jb2 line item has no jobNumber on its mirrored payload"},
        )
        logger.warning(
            "outbox_skipped_no_job_number", extra={"work_order_id": str(unit.work_order_id)}
        )
        return None

    detail = {
        "jobNumber": job_number,
        "stepNumber": plan_op.seq,
        "piecesFinished": pieces_finished,
        "piecesScrapped": pieces_scrapped,
        "reasonNumber": reason_number,
        **_time_fields(session, work_session),
    }
    return {
        "employeeCode": employee_code,
        "ticketDate": _ticket_date(work_session),
        "allowClosedJobs": True,
        "timeTicketDetails": [detail],
    }


def enqueue_finish_writeback(
    session: Session,
    work_session: WorkSession,
    unit: Unit,
    plan_op: PlanOperation,
    *,
    pieces_finished: int = 0,
    pieces_scrapped: int = 0,
    reason_number: int | None = None,
) -> uuid.UUID | None:
    """The one public enqueue entry point for a closed `work_session`:
    ONE `jb2_outbox` row (`kind="time_ticket"`) carrying the full nested
    `POST /time-tickets` body (header + its single `timeTicketDetails[]`
    entry -- CR-018, no separate header row/POST anymore). Returns the
    `jb2_outbox` row id, or `None` if `build_time_ticket` skipped the write
    (unmapped employee/job -- already logged + recorded as a mapping
    exception, never raises). Idempotency key
    `wo:{wo}:unit:{unit_no}:op:{seq}:session:{session_id}` matches
    docs/state-machine.md §5 exactly."""
    payload = build_time_ticket(
        session, work_session, unit, plan_op,
        pieces_finished=pieces_finished, pieces_scrapped=pieces_scrapped,
        reason_number=reason_number,
    )
    if payload is None:
        return None

    key = writer.make_key(
        "wo", unit.work_order_id, "unit", unit.unit_no, "op", plan_op.seq,
        "session", work_session.id,
    )
    outbox_id = writer.enqueue(session, "time_ticket", payload, key, unit.work_order_id)
    events.emit(
        session, "outbox.enqueued", entity=("jb2_outbox", outbox_id),
        actor_id=work_session.operator_id,
        after={
            "kind": "time_ticket", "work_session_id": str(work_session.id),
            "pieces_finished": pieces_finished, "pieces_scrapped": pieces_scrapped,
        },
    )
    return outbox_id
