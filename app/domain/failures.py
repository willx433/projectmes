"""Failure / scrap / rework flows (P3-08, DD §6.5, docs/state-machine.md §5,
override matrix O3-O5). Builds on `app.domain.statemachine`'s `close_session`
(never duplicates it) for the session-closing side effects of scrap and
send-back-to-op-K.

Entry point: `record_failure(...)`. One `Failure` row is always written
(the audit record of "something went wrong here"); the `disposition` then
drives exactly one of four effect paths:

  - `use_as_is`  -- O3 recorded via the fail dialog (not the measurement
    dialog) rather than mutating anything about the unit's route.
  - `rework_in_place` -- the failed substep resets to pending (old row
    superseded, append-only); the owning step's `done` status is rolled
    back too so `POST .../finish` (P3-10) correctly re-blocks until it's
    redone. Session-kind consequences of "rework accrues" fall out of
    `unit.first_pass` flipping permanently (see below) -- no separate
    session-kind field to juggle.
  - `rework_to_op` (O5, lead) -- ops K..current superseded for this unit;
    unit repositioned to op K, `in_transit`; open session(s) on the current
    op close(clocked_out).
  - `scrap` (O4, lead) -- `scrap_events` row (+ best-effort material value
    estimate), unit -> scrapped, box released, optional remake unit; the
    closing session(s) enqueue their time-ticket detail with
    `piecesScrapped=1` (CR-010: this and P3-10's finish path are the only
    two producers of that outbox kind).

`unit.first_pass` flips to `False` permanently -- and `rework_count`
increments -- for BOTH `rework_in_place` and `rework_to_op`: DD §5's Unit
state machine states this as one general rule ("at_station -> rework(op K
<= current) [first_pass=false forever, rework_count++]") without carving
out an in-place exception, and it's what makes a from-here-on WorkSession's
`kind` come out `rework` via `statemachine._session_kind` (which only ever
branches on `unit.first_pass`) without inventing a second signal.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain import events, statemachine
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    FAILURE_DISPOSITIONS,
    BoxAssignment,
    BuildBox,
    Failure,
    Operator,
    ScrapEvent,
    StepExecution,
    SubstepExecution,
    Transit,
    WorkSession,
)
from app.domain.models_jb2 import JB2OrderMaterial
from app.domain.models_library import FailureCode
from app.outbox import payloads


class LeadRequiredError(ValueError):
    """Raised when scrap/rework_to_op is attempted without a lead authorizer."""


def record_failure(
    session: Session,
    unit: Unit,
    plan_op: PlanOperation,
    substep_execution: SubstepExecution | None,
    failure_code_id: uuid.UUID,
    narrative: str | None,
    detected_by: Operator,
    disposition: str,
    *,
    rework_to_op: PlanOperation | None = None,
    authorized_by: Operator | None = None,
    cause_code: str | None = None,
    remake: bool = False,
    now: datetime | None = None,
) -> Failure:
    """Contract O3-O5 / state-machine.md §5. Always records one `Failure`
    row; `disposition` selects the effect path (see module docstring).
    `authorized_by` (a lead) is required for `scrap`/`rework_to_op` --
    raises `LeadRequiredError` (a `ValueError` subclass) otherwise, so a
    bare `except ValueError` at the call site still catches it."""
    if disposition not in FAILURE_DISPOSITIONS:
        raise ValueError(f"unknown disposition: {disposition!r}")
    if disposition == "rework_to_op" and rework_to_op is None:
        raise ValueError("rework_to_op disposition requires a rework_to_op target operation")

    now = now or datetime.now(timezone.utc)

    failure = Failure(
        unit_id=unit.id,
        plan_operation_id=plan_op.id,
        substep_execution_id=substep_execution.id if substep_execution else None,
        failure_code_id=failure_code_id,
        narrative=narrative,
        detected_by=detected_by.id,
        detected_at=now,
        disposition=disposition,
        rework_to_op=rework_to_op.id if rework_to_op else None,
        authorized_by=authorized_by.id if authorized_by else None,
    )
    session.add(failure)
    session.flush()
    events.emit(
        session, "failure.recorded", entity=failure, actor_id=detected_by.id,
        after={
            "unit_id": str(unit.id), "plan_operation_id": str(plan_op.id),
            "disposition": disposition,
        },
    )

    if disposition == "use_as_is":
        _apply_use_as_is(session, failure, substep_execution, authorized_by or detected_by)
    elif disposition == "rework_in_place":
        _apply_rework_in_place(session, unit, failure, substep_execution, detected_by)
    elif disposition == "scrap":
        _require_lead(authorized_by, "scrap")
        _apply_scrap(
            session, unit, failure, authorized_by, now=now, cause_code=cause_code, remake=remake,
        )
    else:  # rework_to_op
        _require_lead(authorized_by, "rework_to_op")
        _apply_rework_to_op(session, unit, plan_op, rework_to_op, authorized_by, failure, now=now)

    return failure


def _require_lead(authorized_by: Operator | None, disposition: str) -> None:
    if authorized_by is None or "lead" not in (authorized_by.roles or []):
        raise LeadRequiredError(f"{disposition} disposition requires a lead authorizer")


# -- O3: use-as-is recorded via the fail dialog -------------------------------


def _apply_use_as_is(
    session: Session, failure: Failure, substep_execution: SubstepExecution | None,
    authorizer: Operator,
) -> None:
    if substep_execution is not None:
        substep_execution.disposition = "use_as_is"
        substep_execution.disposition_by = authorizer.id
        substep_execution.out_of_tolerance = True
        substep_execution.status = "done"
    events.emit(
        session, "disposition.applied", entity=failure, actor_id=authorizer.id,
        after={"disposition": "use_as_is"},
    )


# -- rework-in-place -----------------------------------------------------------


def _apply_rework_in_place(
    session: Session, unit: Unit, failure: Failure,
    substep_execution: SubstepExecution | None, actor: Operator,
) -> None:
    if substep_execution is not None:
        substep_execution.superseded = True
        step_exec = session.get(StepExecution, substep_execution.step_execution_id)
        # Un-blocks P3-10's finish-gate (`_all_steps_done`), which only
        # trusts a *non-superseded* StepExecution row's `status` -- reopening
        # one substep must reopen its parent step too, or finish would
        # wrongly consider the step still done.
        if step_exec is not None and step_exec.status == "done":
            step_exec.status = "in_progress"
            step_exec.completed_at = None
        session.add(
            SubstepExecution(
                step_execution_id=substep_execution.step_execution_id,
                substep_seq=substep_execution.substep_seq,
                type=substep_execution.type,
                status="pending",
                superseded=False,
            )
        )
        session.flush()

    unit.first_pass = False
    unit.rework_count += 1
    events.emit(
        session, "unit.reworked", entity=unit, actor_id=actor.id,
        after={"mode": "in_place", "plan_operation_id": str(failure.plan_operation_id)},
    )
    events.emit(
        session, "disposition.applied", entity=failure, actor_id=actor.id,
        after={"disposition": "rework_in_place"},
    )


# -- O5: send back to op K -----------------------------------------------------


def _reset_ops_range(
    session: Session, unit: Unit, from_op: PlanOperation, to_op: PlanOperation,
) -> None:
    """Marks every non-superseded StepExecution/SubstepExecution for `unit`
    on plan_operations [from_op.seq, to_op.seq] superseded -- append-only
    history (state-machine.md §5), not deletion. Fresh step/substep
    executions for the reopened ops are created the normal way once the
    unit scans back in (existing scan/step flow) -- a superseded-only
    history reads the same as "not started" to the finish-gate."""
    plan_op_ids = session.scalars(
        select(PlanOperation.id).where(
            PlanOperation.work_order_id == unit.work_order_id,
            PlanOperation.seq >= from_op.seq,
            PlanOperation.seq <= to_op.seq,
        )
    ).all()
    if not plan_op_ids:
        return

    step_execs = session.scalars(
        select(StepExecution).where(
            StepExecution.unit_id == unit.id,
            StepExecution.plan_operation_id.in_(plan_op_ids),
            StepExecution.superseded.is_(False),
        )
    ).all()
    for step_exec in step_execs:
        step_exec.superseded = True
        subs = session.scalars(
            select(SubstepExecution).where(
                SubstepExecution.step_execution_id == step_exec.id,
                SubstepExecution.superseded.is_(False),
            )
        ).all()
        for sub in subs:
            sub.superseded = True


def _apply_rework_to_op(
    session: Session, unit: Unit, plan_op: PlanOperation, rework_to_op: PlanOperation,
    authorized_by: Operator, failure: Failure, *, now: datetime,
) -> None:
    if rework_to_op.work_order_id != unit.work_order_id:
        raise ValueError("rework_to_op must be a plan operation on the same work order")
    if rework_to_op.seq > plan_op.seq:
        raise ValueError("rework_to_op target must be at or before the current operation")

    _reset_ops_range(session, unit, rework_to_op, plan_op)

    open_sessions = session.scalars(
        select(WorkSession).where(
            WorkSession.unit_id == unit.id,
            WorkSession.plan_operation_id == plan_op.id,
            WorkSession.ended_at.is_(None),
        )
    ).all()
    from_station_id = None
    for ws in open_sessions:
        from_station_id = from_station_id or ws.station_id
        statemachine.close_session(
            session, ws, reason="clocked_out", actor_id=authorized_by.id, now=now,
        )
        # ponytail: no time-ticket enqueued for the pre-failure labor here --
        # this task's brief scopes outbox writes to the scrap path only (see
        # enqueue_finish_writeback's 3 call sites: finish, scrap, O6
        # lead-confirm). The clocked-out time before the reopened op is
        # redone isn't posted at reopen time in v1; upgrade path is to call
        # payloads.enqueue_finish_writeback(session, ws, unit, plan_op,
        # pieces_finished=0) here too once that's confirmed in-scope.

    unit.first_pass = False
    unit.rework_count += 1
    unit.current_plan_op_id = rework_to_op.id
    unit.status = "in_transit"
    session.add(Transit(unit_id=unit.id, from_station_id=from_station_id, departed_at=now))

    events.emit(
        session, "unit.reworked", entity=unit, actor_id=authorized_by.id,
        after={
            "mode": "send_back", "rework_to_op": str(rework_to_op.id),
            "rework_count": unit.rework_count,
        },
    )
    events.emit(
        session, "disposition.applied", entity=failure, actor_id=authorized_by.id,
        after={"disposition": "rework_to_op"},
    )


# -- O4: scrap ------------------------------------------------------------------


def _reason_number(session: Session, failure_code_id: uuid.UUID) -> int | None:
    fc = session.get(FailureCode, failure_code_id)
    return fc.jb2_reason_number if fc else None


def _estimate_material_value(
    session: Session, unit: Unit, plan_operation_id: uuid.UUID,
) -> float | None:
    """ponytail: heuristic, not exact costing -- sums `jb2_order_materials`
    (qty_planned * unit_cost) for the work order's line item across every
    routing step up to and including the op where the failure was
    detected (assumes later ops' material hasn't been consumed yet).
    Returns None (not 0) when no cost data resolves at all, so callers can
    tell "estimated zero" apart from "couldn't estimate." Upgrade path:
    switch to actual `material_records.qty_used` once P3-12 material
    tracking is live, instead of the JB2 plan estimate."""
    work_order = session.get(WorkOrder, unit.work_order_id)
    if work_order is None:
        return None
    plan_op = session.get(PlanOperation, plan_operation_id)
    max_seq = plan_op.seq if plan_op else None

    materials = session.scalars(
        select(JB2OrderMaterial).where(
            JB2OrderMaterial.jb2_line_item_id == work_order.jb2_line_item_id
        )
    ).all()
    total = None
    for m in materials:
        if max_seq is not None and m.routing_seq is not None and m.routing_seq > max_seq:
            continue
        if m.qty_planned is None or m.unit_cost is None:
            continue
        total = (total or 0.0) + float(m.qty_planned) * float(m.unit_cost)
    return total


def _release_box(session: Session, unit: Unit, now: datetime) -> None:
    box = session.execute(
        select(BuildBox).where(BuildBox.current_unit_id == unit.id)
    ).scalars().first()
    if box is None:
        return
    assignment = session.execute(
        select(BoxAssignment)
        .where(
            BoxAssignment.box_id == box.id, BoxAssignment.unit_id == unit.id,
            BoxAssignment.released_at.is_(None),
        )
        .order_by(BoxAssignment.assigned_at.desc())
    ).scalars().first()
    if assignment is not None:
        assignment.released_at = now
    box.current_unit_id = None
    session.flush()
    events.emit(session, "box.released", entity=box, after={"unit_id": str(unit.id)})


def _create_remake_unit(session: Session, unit: Unit) -> Unit:
    max_unit_no = (
        session.execute(
            select(func.max(Unit.unit_no)).where(Unit.work_order_id == unit.work_order_id)
        ).scalar()
        or 0
    )
    replacement = Unit(
        work_order_id=unit.work_order_id,
        unit_no=max_unit_no + 1,
        serial_number=None,
        status="queued",
        first_pass=True,
        rework_count=0,
        remake_of_unit_id=unit.id,
    )
    session.add(replacement)
    session.flush()
    return replacement


def _apply_scrap(
    session: Session, unit: Unit, failure: Failure, authorized_by: Operator, *,
    now: datetime, cause_code: str | None, remake: bool,
) -> ScrapEvent:
    reason_number = _reason_number(session, failure.failure_code_id)
    material_value_est = _estimate_material_value(session, unit, failure.plan_operation_id)

    scrap_event = ScrapEvent(
        unit_id=unit.id, failure_id=failure.id, cause_code=cause_code, narrative=failure.narrative,
        material_value_est=material_value_est, authorized_by=authorized_by.id, created_at=now,
    )
    session.add(scrap_event)
    session.flush()

    plan_op = session.get(PlanOperation, failure.plan_operation_id)
    open_sessions = session.scalars(
        select(WorkSession).where(
            WorkSession.unit_id == unit.id,
            WorkSession.plan_operation_id == plan_op.id,
            WorkSession.ended_at.is_(None),
        )
    ).all()
    for ws in open_sessions:
        statemachine.close_session(
            session, ws, reason="finished", actor_id=authorized_by.id, now=now,
        )
        # CR-010: time-ticket details are the sole JB2 write-back -- the
        # scrapped piece count rides this closing detail, no routing PATCH.
        payloads.enqueue_finish_writeback(
            session, ws, unit, plan_op, pieces_finished=0, pieces_scrapped=1,
            reason_number=reason_number,
        )

    unit.status = "scrapped"
    _release_box(session, unit, now)

    replacement_id = None
    if remake:
        replacement = _create_remake_unit(session, unit)
        scrap_event.replacement_unit_id = replacement.id
        replacement_id = replacement.id

    session.flush()
    events.emit(
        session, "unit.scrapped", entity=unit, actor_id=authorized_by.id,
        after={
            "failure_id": str(failure.id), "scrap_event_id": str(scrap_event.id),
            "replacement_unit_id": str(replacement_id) if replacement_id else None,
        },
    )
    events.emit(
        session, "disposition.applied", entity=failure, actor_id=authorized_by.id,
        after={"disposition": "scrap"},
    )
    return scrap_event
