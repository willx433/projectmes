"""Finish-operation + session lifecycle endpoints (P3-10/P3-08/09, DD
§6.6/§6.8, docs/state-machine.md §5/§6). All validation/mutation happens
through `app.domain.statemachine` primitives + `app.domain.sessions` +
`app.domain.failures`; this module resolves station/operator from the
request, shapes the HTTP response, and is the one place that calls
`app.outbox.payloads.enqueue_finish_writeback` for a normal finish.

FIXED ENDPOINT CONTRACT (do not rename/reshape):
`POST /operations/{plan_op_id}/finish {unit_id, request_id}` -- the station
UI (a concurrent agent's work) posts here. Substep-level endpoints
(`app/api/substeps.py`) are not this module's concern.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import deps
from app.config import config
from app.db import get_session
from app.domain import events, statemachine
from app.domain import sessions as sessions_domain
from app.domain.models_execution import PlanOperation, Unit
from app.domain.models_floor import Station, StepExecution, Transit, WorkSession
from app.outbox import payloads

router = APIRouter()


class FinishBody(BaseModel):
    unit_id: uuid.UUID
    request_id: str | None = None


class PauseBody(BaseModel):
    reason: str
    request_id: str | None = None


class ResumeBody(BaseModel):
    reason: str | None = None  # ponytail: symmetry w/ pause; unused (resume has no reason enum)
    request_id: str | None = None


def _all_steps_done(session: Session, unit: Unit, plan_op: PlanOperation) -> bool:
    """Contract §5: "Operation finishable when all steps done -- server
    checked." Steps come from the frozen plan content (`plan_operations
    .frozen_content['steps']`); a step counts as done for this unit when a
    non-superseded `step_executions` row for its seq is `status='done'`
    (state-machine.md §2 -- the per-unit op-state resolution). A plan op
    with no steps at all (e.g. the `blocked_no_instructions` placeholder)
    has nothing to gate on."""
    steps = (plan_op.frozen_content or {}).get("steps") or []
    if not steps:
        return True
    required_seqs = {s["seq"] for s in steps}
    done_seqs = set(
        session.scalars(
            select(StepExecution.step_seq).where(
                StepExecution.unit_id == unit.id,
                StepExecution.plan_operation_id == plan_op.id,
                StepExecution.status == "done",
                StepExecution.superseded.is_(False),
            )
        ).all()
    )
    return required_seqs.issubset(done_seqs)


def _peek_next_op(session: Session, unit: Unit, plan_op: PlanOperation) -> PlanOperation | None:
    """Read-only lookahead at the same next-non-skipped-op query
    `statemachine.unit_op_done` performs -- needed here so the last-op
    serial gate (§6.8) can be checked *before* any mutation happens, while
    the actual pointer advance still goes through `unit_op_done` itself
    (never duplicated as a write, only as this read)."""
    return session.scalars(
        select(PlanOperation)
        .where(
            PlanOperation.work_order_id == unit.work_order_id,
            PlanOperation.seq > plan_op.seq,
            PlanOperation.status != "skipped",
        )
        .order_by(PlanOperation.seq)
    ).first()


@router.post("/operations/{plan_op_id}/finish")
def finish_operation(
    plan_op_id: uuid.UUID,
    body: FinishBody,
    station: Station = Depends(deps.require_station),
    operator=Depends(deps.require_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        plan_op = session.get(PlanOperation, plan_op_id)
        if plan_op is None:
            raise HTTPException(status_code=404, detail="plan operation not found")
        unit = statemachine.lock_unit(session, body.unit_id)
        if unit is None:
            raise HTTPException(status_code=404, detail="unit not found")

        # §17.9 idempotent double-finish: the unit already advanced past
        # this op (a prior finish call already ran) -- no-op, not an error.
        if unit.current_plan_op_id != plan_op.id:
            return {
                "code": "already_finished", "unit_id": str(unit.id), "unit_status": unit.status,
            }

        if not _all_steps_done(session, unit, plan_op):
            raise HTTPException(
                status_code=409, detail="operation not finishable: required steps incomplete"
            )

        next_op = _peek_next_op(session, unit, plan_op)
        # v1 simplification (serial gate is global, not per-product -- see
        # task report): REQUIRE_SERIAL_BEFORE_DONE gates every product the
        # same way; DD §6.8's per-product config is deferred.
        if next_op is None and config.require_serial_before_done and not unit.serial_number:
            raise HTTPException(
                status_code=422, detail="serial number required before unit can be marked done"
            )

        now = datetime.now(timezone.utc)
        open_sessions = session.scalars(
            select(WorkSession).where(
                WorkSession.unit_id == unit.id, WorkSession.plan_operation_id == plan_op.id,
                WorkSession.ended_at.is_(None),
            )
        ).all()
        outbox_ids: list[str] = []
        for ws in open_sessions:
            statemachine.close_session(
                session, ws, reason="finished", actor_id=operator.id, now=now,
            )
            # v1 simplification: one unit per finish call (batch qty>1
            # per-unit outcomes are §17.11, deferred -- see task report).
            outbox_id = payloads.enqueue_finish_writeback(
                session, ws, unit, plan_op, pieces_finished=1, pieces_scrapped=0,
            )
            if outbox_id is not None:
                outbox_ids.append(str(outbox_id))

        advanced = statemachine.unit_op_done(session, unit, plan_op)
        if advanced is not None:
            unit.status = "in_transit"
            session.add(Transit(unit_id=unit.id, from_station_id=station.id, departed_at=now))
            events.emit(
                session, "unit.moved", entity=unit, actor_id=operator.id, station_id=station.id,
                after={
                    "from_plan_operation_id": str(plan_op.id),
                    "to_plan_operation_id": str(advanced.id),
                },
            )
        else:
            unit.status = "done"
            unit.completed_at = now
            events.emit(
                session, "unit.done", entity=unit, actor_id=operator.id, station_id=station.id,
                after={},
            )

        return {
            "code": "finished", "unit_id": str(unit.id), "unit_status": unit.status,
            "outbox_ids": outbox_ids,
        }

    result = statemachine.with_request_dedup(
        session, body.request_id, "POST /operations/finish", _run,
    )
    session.commit()
    return result


@router.post("/sessions/{session_id}/pause")
def pause_session_endpoint(
    session_id: uuid.UUID,
    body: PauseBody,
    station: Station = Depends(deps.require_station),
    operator=Depends(deps.require_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    work_session = session.get(WorkSession, session_id)
    if work_session is None:
        raise HTTPException(status_code=404, detail="work session not found")
    try:
        statemachine.pause_session(
            session, work_session, reason_code=body.reason, actor_id=operator.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return {"code": "paused", "session_id": str(work_session.id)}


@router.post("/sessions/{session_id}/resume")
def resume_session_endpoint(
    session_id: uuid.UUID,
    body: ResumeBody,
    station: Station = Depends(deps.require_station),
    operator=Depends(deps.require_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    work_session = session.get(WorkSession, session_id)
    if work_session is None:
        raise HTTPException(status_code=404, detail="work session not found")
    try:
        statemachine.resume_session(session, work_session, actor_id=operator.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return {"code": "resumed", "session_id": str(work_session.id)}


@router.post("/sessions/{session_id}/confirm")
def confirm_session_endpoint(
    session_id: uuid.UUID,
    lead=Depends(deps.require_role("lead")),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """O6 (state-machine.md §4): a lead badges in (station + operator
    session, role `lead`) and confirms an `auto_closed` session -- this is
    the moment its time-ticket detail enqueues, not at auto-close."""
    work_session = session.get(WorkSession, session_id)
    if work_session is None:
        raise HTTPException(status_code=404, detail="work session not found")
    try:
        sessions_domain.lead_confirm_session(session, work_session, lead)
    except sessions_domain.LeadRequiredError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return {"code": "confirmed", "session_id": str(work_session.id)}
