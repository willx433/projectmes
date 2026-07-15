"""JB2 write-back payload builders (P3-10, DD §4.4 as amended by CR-010,
docs/jb2-api-findings.md §2). Time-ticket details are the **sole** JB2
write-back for operation completion/scrap -- there is no routing-step PATCH
(CR-010: `OrderRoutingUpdate` is `additionalProperties:false` and doesn't
carry actuals/status at all). Every enqueue in this module rides the
existing `time_ticket`/`time_ticket_detail` outbox kinds, which
`app/outbox/drainer.py`'s `DEFAULT_SENDERS` already posts to
`POST /time-tickets` / `POST /time-ticket-details` verbatim -- no new
sender registration needed here.

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
    JB2OrderRouting,
    JB2WorkCenter,
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


def _op_work_center_code(session: Session, plan_op: PlanOperation) -> str | None:
    if plan_op.jb2_routing_id is None:
        return None
    routing = session.get(JB2OrderRouting, plan_op.jb2_routing_id)
    return routing.work_center_code if routing else None


def _work_center_int(session: Session, plan_op: PlanOperation) -> int | None:
    """`TimeTicketDetailCreate.workCenter` is `int32` (findings §2 note --
    unlike the string work-center codes used elsewhere, e.g.
    `OrderRoutingUpdate.workCenter`). Best-effort resolution via the
    `jb2_work_centers` mirror's own numeric `jb2_id`; nullable in the JB2
    schema, so returning None on any miss is a safe degrade, not a block."""
    code = _op_work_center_code(session, plan_op)
    if not code:
        return None
    wc = session.execute(select(JB2WorkCenter).where(JB2WorkCenter.code == code)).scalars().first()
    if wc is None:
        return None
    try:
        return int(wc.jb2_id)
    except (TypeError, ValueError):
        return None


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


def _time_fields(session: Session, work_session: WorkSession) -> dict:
    """**THE isolated choice** (docs/jb2-api-findings.md §2 / risk R5):
    whether JB2 wants `timeStart`/`timeEnd` or `setupTime`/`cycleTime` --
    and whether the two pairs are alternates or complements -- is
    BLOCKED-ON-WRITE-ACCESS (no dummy job / write approval as of this
    task; P0-R1 is the remediation task that closes it out with live
    evidence). This function is the ONLY place that decision is made --
    when P0-R1 lands, change the branch here and nowhere else.

    DEFAULT (this function, per this task's brief): `timeStart`/`timeEnd`
    for first_pass/rework sessions; `setupTime` (elapsed hours) for
    `kind='setup'` sessions -- matching DD §4.4's write-backs row ("or
    setupTime/cycleTime") and the task brief's stated default. `cycleTime`
    is left null throughout -- nothing in this codebase distinguishes
    machine-cycle time from wall-clock session time yet.
    """
    elapsed_hours = _elapsed_hours(session, work_session)
    if work_session.kind == "setup":
        return {"setupTime": elapsed_hours, "cycleTime": None, "timeStart": None, "timeEnd": None}
    return {
        "timeStart": format_jb2_datetime(work_session.started_at),
        "timeEnd": format_jb2_datetime(work_session.ended_at) if work_session.ended_at else None,
        "setupTime": None,
        "cycleTime": None,
    }


def build_time_ticket_detail(
    session: Session,
    work_session: WorkSession,
    unit: Unit,
    plan_op: PlanOperation,
    *,
    pieces_finished: int = 0,
    pieces_scrapped: int = 0,
    reason_number: int | None = None,
) -> dict | None:
    """Builds the `TimeTicketDetailCreate` body for one closed `work_session`.

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

    payload = {
        "employeeCode": employee_code,
        "jobNumber": job_number,
        "ticketDate": _ticket_date(work_session),
        "stepNumber": plan_op.seq,
        "workCenter": _work_center_int(session, plan_op),
        "piecesFinished": pieces_finished,
        "piecesScrapped": pieces_scrapped,
        "reasonNumber": reason_number,
        **_time_fields(session, work_session),
    }
    return payload


def ensure_time_ticket_header(
    session: Session, employee_code: int, ticket_date: str, work_order_id: uuid.UUID | None,
) -> uuid.UUID:
    """`POST /time-tickets` header, enqueued once per (employee, date).
    `writer.enqueue`'s own idempotency-key dedup (a plain check-then-insert
    on `jb2_outbox.idempotency_key`) already IS the "check for an existing
    `tt:{emp}:{date}` row" the task brief asks for -- reusing it here is
    simpler than a second existence check before calling the same function."""
    key = writer.make_key("tt", employee_code, ticket_date)
    payload = {"employeeCode": employee_code, "ticketDate": ticket_date}
    return writer.enqueue(session, "time_ticket", payload, key, work_order_id)


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
    """The one public enqueue entry point for a closed `work_session`'s
    time-ticket detail (+ its header, ensured first). Returns the
    `jb2_outbox` row id, or `None` if `build_time_ticket_detail` skipped
    the write (unmapped employee/job -- already logged + recorded as a
    mapping exception, never raises). Idempotency key
    `wo:{wo}:unit:{unit_no}:op:{seq}:session:{session_id}` matches
    docs/state-machine.md §5 exactly."""
    detail_payload = build_time_ticket_detail(
        session, work_session, unit, plan_op,
        pieces_finished=pieces_finished, pieces_scrapped=pieces_scrapped,
        reason_number=reason_number,
    )
    if detail_payload is None:
        return None

    ensure_time_ticket_header(
        session, detail_payload["employeeCode"], detail_payload["ticketDate"], unit.work_order_id,
    )

    key = writer.make_key(
        "wo", unit.work_order_id, "unit", unit.unit_no, "op", plan_op.seq,
        "session", work_session.id,
    )
    outbox_id = writer.enqueue(
        session, "time_ticket_detail", detail_payload, key, unit.work_order_id
    )
    events.emit(
        session, "outbox.enqueued", entity=("jb2_outbox", outbox_id),
        actor_id=work_session.operator_id,
        after={
            "kind": "time_ticket_detail", "work_session_id": str(work_session.id),
            "pieces_finished": pieces_finished, "pieces_scrapped": pieces_scrapped,
        },
    )
    return outbox_id
