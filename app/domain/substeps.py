"""Substep/step execution domain logic (P3-06/P3-07/P3-12).

Governing contract: docs/state-machine.md §5 (substep/step/operation rules),
§4 (override matrix O1/O3/O4/O5/O8). Same split as app/domain/statemachine.py
/ app/api/scan.py: this module is the single server-side validator for every
substep-level mutation; app/api/substeps.py only resolves the request
(station/operator/badge) and calls functions here.

`step_executions` rows are created lazily per (plan_operation_id, unit_id,
step_seq) the first time any of that step's substeps is touched (§2: no
new table, `plan_operations.frozen_content` is the read model). A step
flips to `done` automatically the moment its last required substep reaches
done/skipped -- there is no separate "finish step" action; §5's "cannot
finish a step with required substeps open" is enforced by construction
(nothing else sets `step_executions.status = 'done'`).

Out-of-tolerance measurements block the substep in `failed` until one of
the four dispositions (§4 O3/O4/O5, contract §5) is applied via
`apply_disposition`. All four now delegate their unit-of-record write to
`app.domain.failures.record_failure` (P3-R2/ESC-004): the substep-level
disposition dialog collects a `failure_code_id` (DD §9.4's "every
out-of-tolerance disposition is recorded") and, for `rework_to_op`, a
target operation seq, so this module can hand every disposition off to the
same single effect-implementation `record_failure` already has for the
whole-operation Fail screen (app/api/station.py) instead of re-implementing
`use_as_is`/`rework_in_place`'s state changes a second time here. This
module still owns the substep-row bookkeeping (`disposition`/
`disposition_by`/`notes`, and the finish-gate recompute) since
`record_failure` has no reason to know about `step_executions`. Same scope
boundary for generic `fail_substep` (non-measurement failures, e.g. a
failed inspection) -- this module only records the failed state +
narrative; that path has no disposition step at all (a plain fail, not an
out-of-tolerance one) so it doesn't call `record_failure`.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import config
from app.domain import events, failures, statemachine
from app.domain.models_execution import PlanOperation, Unit
from app.domain.models_floor import (
    FAILURE_DISPOSITIONS,
    Attachment,
    MaterialRecord,
    Measurement,
    Operator,
    Station,
    StepExecution,
    SubstepExecution,
    WorkSession,
)

# state-machine.md §4: which role(s) may authorize each disposition.
# Absent from this map (rework_in_place) = no second badge required --
# it's just "try the measurement again", any operator may trigger it.
DISPOSITION_ROLES: dict[str, tuple[str, ...]] = {
    "use_as_is": ("lead", "quality"),
    "rework_to_op": ("lead",),
    "scrap": ("lead",),
}


class SubstepError(Exception):
    """Rejected substep mutation -- app/api/substeps.py maps this to a
    redirect-with-error (matches the rest of the app's admin-form UX, not
    scan.py's JSON-API 403 style, since these are plain-form kiosk posts)."""


@dataclass
class Ctx:
    unit: Unit
    plan_op: PlanOperation
    frozen_step: dict[str, Any]
    frozen_sub: dict[str, Any]
    step_exec: StepExecution
    sub_exec: SubstepExecution
    work_session: WorkSession


# -- frozen_content lookups ---------------------------------------------------


def _frozen_step(plan_op: PlanOperation, step_seq: int) -> dict[str, Any]:
    for step in plan_op.frozen_content.get("steps", []):
        if step["seq"] == step_seq:
            return step
    raise SubstepError(f"no step {step_seq} in this operation")


def _frozen_substep(step: dict[str, Any], substep_seq: int) -> dict[str, Any]:
    for sub in step.get("substeps", []):
        if sub["seq"] == substep_seq:
            return sub
    raise SubstepError(f"no substep {substep_seq} in step {step['seq']}")


def peek_frozen_substep(
    session: Session, unit_id: uuid.UUID, step_seq: int, substep_seq: int
) -> dict[str, Any]:
    """Read-only lookup used by the API layer to learn a substep's type/
    signoff_role *before* running a second-badge check (which role to ask
    for depends on the frozen content)."""
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise SubstepError("unit not found")
    plan_op = statemachine.unit_next_op(session, unit)
    if plan_op is None:
        raise SubstepError("unit has no active operation")
    return _frozen_substep(_frozen_step(plan_op, step_seq), substep_seq)


# -- lazy execution rows -------------------------------------------------------


def get_or_create_step_execution(
    session: Session, plan_op: PlanOperation, unit: Unit, step_seq: int
) -> StepExecution:
    existing = session.execute(
        select(StepExecution).where(
            StepExecution.plan_operation_id == plan_op.id,
            StepExecution.unit_id == unit.id,
            StepExecution.step_seq == step_seq,
            StepExecution.superseded.is_(False),
        )
    ).scalars().first()
    if existing is not None:
        return existing
    _frozen_step(plan_op, step_seq)  # 404s an unknown step_seq before insert
    step_exec = StepExecution(
        plan_operation_id=plan_op.id, unit_id=unit.id, step_seq=step_seq, status="pending",
    )
    session.add(step_exec)
    session.flush()
    return step_exec


def get_or_create_substep_execution(
    session: Session, step_exec: StepExecution, substep_seq: int, sub_type: str
) -> SubstepExecution:
    existing = session.execute(
        select(SubstepExecution).where(
            SubstepExecution.step_execution_id == step_exec.id,
            SubstepExecution.substep_seq == substep_seq,
            SubstepExecution.superseded.is_(False),
        )
    ).scalars().first()
    if existing is not None:
        return existing
    sub_exec = SubstepExecution(
        step_execution_id=step_exec.id, substep_seq=substep_seq, type=sub_type, status="pending",
    )
    session.add(sub_exec)
    session.flush()
    return sub_exec


def _resolve(
    session: Session,
    *,
    station: Station,
    operator: Operator,
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
) -> Ctx:
    """§5: "substep completion requires an open WorkSession on (unit, op) by
    the acting operator" -- resolved here so every mutator below gets it for
    free."""
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise SubstepError("unit not found")
    plan_op = statemachine.unit_next_op(session, unit)
    if plan_op is None:
        raise SubstepError("unit has no active operation")

    frozen_step = _frozen_step(plan_op, step_seq)
    frozen_sub = _frozen_substep(frozen_step, substep_seq)

    work_session = open_work_session(
        session, unit=unit, plan_op=plan_op, operator=operator, station=station
    )
    if work_session is None:
        raise SubstepError(
            "no open work session for this operator on this unit/operation -- scan the box first"
        )

    step_exec = get_or_create_step_execution(session, plan_op, unit, step_seq)
    sub_exec = get_or_create_substep_execution(session, step_exec, substep_seq, frozen_sub["type"])
    return Ctx(unit, plan_op, frozen_step, frozen_sub, step_exec, sub_exec, work_session)


def _recompute_step_status(
    session: Session, step_exec: StepExecution, frozen_step: dict, *, now: datetime
) -> None:
    """§5: "Step done when all required substeps done/skipped." No other
    code path sets step_exec.status = 'done' -- this is the enforcement."""
    subs = session.execute(
        select(SubstepExecution).where(
            SubstepExecution.step_execution_id == step_exec.id,
            SubstepExecution.superseded.is_(False),
        )
    ).scalars().all()
    by_seq = {s.substep_seq: s for s in subs}

    required = [s for s in frozen_step.get("substeps", []) if s.get("required", True)]
    all_required_resolved = all(
        by_seq.get(fs["seq"]) is not None and by_seq[fs["seq"]].status in ("done", "skipped")
        for fs in required
    )

    if all_required_resolved:
        if step_exec.status != "done":
            step_exec.status = "done"
            step_exec.completed_at = now
    else:
        if step_exec.status == "pending" and subs:
            step_exec.status = "in_progress"
        if step_exec.started_at is None and subs:
            step_exec.started_at = now


def step_status_map(session: Session, plan_op: PlanOperation, unit: Unit) -> dict[int, str]:
    rows = session.execute(
        select(StepExecution.step_seq, StepExecution.status).where(
            StepExecution.plan_operation_id == plan_op.id,
            StepExecution.unit_id == unit.id,
            StepExecution.superseded.is_(False),
        )
    ).all()
    return {seq: status for seq, status in rows}


def substep_execution_map(
    session: Session, plan_op: PlanOperation, unit: Unit
) -> dict[tuple[int, int], SubstepExecution]:
    """`(step_seq, substep_seq) -> SubstepExecution` for every non-superseded
    substep touched so far -- the station execute screen's render context
    (status, value_numeric, out_of_tolerance, disposition, skip_reason
    all live on the row) and the finish-gating count below."""
    rows = session.execute(
        select(StepExecution.step_seq, SubstepExecution)
        .join(SubstepExecution, SubstepExecution.step_execution_id == StepExecution.id)
        .where(
            StepExecution.plan_operation_id == plan_op.id,
            StepExecution.unit_id == unit.id,
            SubstepExecution.superseded.is_(False),
        )
    ).all()
    return {(step_seq, sub.substep_seq): sub for step_seq, sub in rows}


def remaining_required_count(session: Session, plan_op: PlanOperation, unit: Unit) -> int:
    """Required substeps, across the WHOLE operation, not yet done/skipped.
    Exposed for: (a) this module's own templates (Finish button disabled
    state), (b) the finish-operation endpoint (P3-10, app/api/operations.py,
    not this task) -- it should call this rather than reimplement the walk."""
    exec_map = substep_execution_map(session, plan_op, unit)
    remaining = 0
    for step in plan_op.frozen_content.get("steps", []):
        for sub in step.get("substeps", []):
            if not sub.get("required", True):
                continue
            row = exec_map.get((step["seq"], sub["seq"]))
            status = row.status if row is not None else "pending"
            if status not in ("done", "skipped"):
                remaining += 1
    return remaining


def open_work_session(
    session: Session, *, unit: Unit, plan_op: PlanOperation, operator: Operator, station: Station
) -> WorkSession | None:
    """The acting operator's currently-open session on (unit, plan_op) at
    this station, if any -- shared lookup for the station GET screens
    (execute/scan-result) and the footer's pause/resume/clock-out forms."""
    return session.execute(
        select(WorkSession).where(
            WorkSession.unit_id == unit.id,
            WorkSession.plan_operation_id == plan_op.id,
            WorkSession.operator_id == operator.id,
            WorkSession.station_id == station.id,
            WorkSession.ended_at.is_(None),
        )
    ).scalars().first()


def current_step_seq(session: Session, plan_op: PlanOperation, unit: Unit) -> int | None:
    """First step (by seq) not yet done -- the execution screen's default
    "current step" when the caller doesn't pin one via ?step=."""
    status_map = step_status_map(session, plan_op, unit)
    steps = sorted(plan_op.frozen_content.get("steps", []), key=lambda s: s["seq"])
    for step in steps:
        if status_map.get(step["seq"]) != "done":
            return step["seq"]
    return steps[-1]["seq"] if steps else None


# -- generic action/inspection completion --------------------------------------


def start_substep(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.status == "pending":
        sub.status = "in_progress"
        sub.started_at = now
        sub.operator_id = operator.id
        session.flush()
    # no VALID_VERBS entry for "substep.started" (state-machine.md §9) --
    # in_progress is a bookkeeping detail, not an auditable transition.
    return {"status": sub.status}


def complete_substep(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, notes: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Action/inspection-pass: the plain "mark done" control."""
    now = now or datetime.now(timezone.utc)
    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.type not in ("action", "inspection"):
        raise SubstepError(f"substep type {sub.type!r} must use its dedicated endpoint")
    if sub.status in ("done", "skipped"):
        return {"status": sub.status}  # idempotent no-op

    if sub.started_at is None:
        sub.started_at = now
    sub.status = "done"
    sub.completed_at = now
    sub.operator_id = operator.id
    sub.notes = notes
    events.emit(
        session, "substep.done", entity=sub, actor_id=operator.id, station_id=station.id,
        after={"unit_id": str(unit_id), "type": sub.type},
    )
    session.flush()
    _recompute_step_status(session, ctx.step_exec, ctx.frozen_step, now=now)
    return {"status": "done"}


def fail_substep(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, notes: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Generic fail (e.g. a failed visual inspection, or the footer's Fail
    control for a non-measurement substep). Records the failed state +
    narrative only -- the full failure-code/disposition/rework deep flow
    (DD §6.5) is P3-08's app/domain/failures.py, not built at the time of
    this task. ponytail: seam, not a shortcut -- see module docstring."""
    now = now or datetime.now(timezone.utc)
    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.status in ("done", "skipped"):
        raise SubstepError("substep already resolved")

    sub.status = "failed"
    sub.notes = notes
    sub.operator_id = operator.id
    events.emit(
        session, "substep.failed", entity=sub, actor_id=operator.id, station_id=station.id,
        after={"unit_id": str(unit_id), "notes": notes},
    )
    session.flush()
    _recompute_step_status(session, ctx.step_exec, ctx.frozen_step, now=now)
    return {"status": "failed"}


def skip_substep(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, skip_reason: str, lead: Operator, now: datetime | None = None,
) -> dict[str, Any]:
    """O1: skip a required substep. `lead` must already be validated by the
    caller (app/api/substeps.py calls deps.second_badge(role='lead') first,
    same split as scan.py's override_by)."""
    now = now or datetime.now(timezone.utc)
    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.status in ("done", "skipped"):
        return {"status": sub.status}  # idempotent no-op

    sub.status = "skipped"
    sub.skip_reason = skip_reason
    sub.skip_authorized_by = lead.id
    sub.operator_id = sub.operator_id or operator.id
    events.emit(
        session, "substep.skipped", entity=sub, actor_id=operator.id, station_id=station.id,
        after={
            "unit_id": str(unit_id), "skip_reason": skip_reason,
            "skip_authorized_by": str(lead.id),
        },
    )
    session.flush()
    _recompute_step_status(session, ctx.step_exec, ctx.frozen_step, now=now)
    return {"status": "skipped"}


# -- measurement (P3-07) --------------------------------------------------------


def _in_tolerance(value: Decimal, spec: dict[str, Any]) -> bool:
    nominal = Decimal(str(spec["nominal"]))
    tol_plus = Decimal(str(spec.get("tol_plus") or 0))
    tol_minus = Decimal(str(spec.get("tol_minus") or 0))
    lower = nominal - abs(tol_minus)
    upper = nominal + abs(tol_plus)
    return lower <= value <= upper


def record_measurement(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, value: str | float, gauge_id: str | None = None,
    notes: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """§5/§6.4: measurement in tolerance -> substep done. Out of tolerance ->
    substep BLOCKED in `failed`-pending-disposition until `apply_disposition`
    is called (contract's "no disposition -> step cannot complete")."""
    now = now or datetime.now(timezone.utc)
    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.type != "measurement":
        raise SubstepError("substep is not a measurement type")
    if sub.status in ("done", "skipped"):
        return {"status": sub.status, "in_tolerance": sub.pass_}

    spec = ctx.frozen_sub.get("measurement_spec") or {}
    if "nominal" not in spec:
        raise SubstepError("measurement substep has no measurement_spec.nominal")
    try:
        value_dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise SubstepError("value must be numeric") from exc

    in_tol = _in_tolerance(value_dec, spec)

    if sub.started_at is None:
        sub.started_at = now
    sub.operator_id = operator.id
    sub.value_numeric = value_dec
    sub.out_of_tolerance = not in_tol
    sub.pass_ = in_tol
    sub.notes = notes

    measurement = Measurement(
        substep_execution_id=sub.id, name=ctx.frozen_sub["title"], unit=spec.get("unit"),
        value=value_dec, nominal=spec.get("nominal"), tol_plus=spec.get("tol_plus"),
        tol_minus=spec.get("tol_minus"), in_tolerance=in_tol, gauge_id=gauge_id,
        recorded_by=operator.id, recorded_at=now,
    )
    session.add(measurement)
    session.flush()
    events.emit(
        session, "measurement.recorded", entity=measurement, actor_id=operator.id,
        station_id=station.id,
        after={
            "unit_id": str(unit_id), "substep_execution_id": str(sub.id),
            "value": str(value_dec), "in_tolerance": in_tol,
        },
    )

    if in_tol:
        sub.status = "done"
        sub.completed_at = now
        events.emit(
            session, "substep.done", entity=sub, actor_id=operator.id, station_id=station.id,
            after={"unit_id": str(unit_id), "measurement_id": str(measurement.id)},
        )
    else:
        sub.status = "failed"
        events.emit(
            session, "substep.failed", entity=sub, actor_id=operator.id, station_id=station.id,
            after={
                "unit_id": str(unit_id), "measurement_id": str(measurement.id),
                "reason": "out_of_tolerance",
            },
        )
    session.flush()
    _recompute_step_status(session, ctx.step_exec, ctx.frozen_step, now=now)
    return {"status": sub.status, "in_tolerance": in_tol, "substep_execution_id": str(sub.id)}


def apply_disposition(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, disposition: str, failure_code_id: uuid.UUID,
    notes: str | None = None, authorizer: Operator | None = None,
    rework_to_op_seq: int | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """§4 O3/O4/O5: dispose an out-of-tolerance measurement. `authorizer`
    must already be validated by the caller for dispositions that need one
    (DISPOSITION_ROLES) -- `rework_in_place` needs none.

    Delegates the actual effect to `app.domain.failures.record_failure`
    (P3-R2) for all four dispositions -- one `Failure` row + one effect
    implementation, instead of this module hand-rolling `use_as_is`/
    `rework_in_place`'s state changes a second time and leaving
    `rework_to_op`/`scrap` as substep-only flags nobody acted on. This
    function keeps only the substep-row bookkeeping `record_failure` has no
    reason to know about (`disposition`/`disposition_by`/`notes`) and the
    finish-gate recompute."""
    now = now or datetime.now(timezone.utc)
    if disposition not in FAILURE_DISPOSITIONS:
        raise SubstepError(f"unknown disposition {disposition!r}")

    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.status != "failed" or not sub.out_of_tolerance:
        raise SubstepError("substep has no pending out-of-tolerance disposition")

    rework_to_op = None
    if disposition == "rework_to_op":
        if rework_to_op_seq is None:
            raise SubstepError("rework_to_op disposition requires a target operation")
        rework_to_op = session.execute(
            select(PlanOperation).where(
                PlanOperation.work_order_id == ctx.unit.work_order_id,
                PlanOperation.seq == rework_to_op_seq,
            )
        ).scalars().first()
        if rework_to_op is None:
            raise SubstepError(f"no operation at seq {rework_to_op_seq} on this work order")

    sub.disposition = disposition
    sub.disposition_by = authorizer.id if authorizer else operator.id
    if notes:
        sub.notes = notes

    try:
        failures.record_failure(
            session, ctx.unit, ctx.plan_op, sub, failure_code_id, notes, operator,
            disposition, rework_to_op=rework_to_op, authorized_by=authorizer, now=now,
        )
    except (failures.LeadRequiredError, ValueError) as exc:
        raise SubstepError(str(exc)) from exc

    if disposition == "use_as_is":
        # record_failure's _apply_use_as_is already set status="done"; the
        # completed_at timestamp is substep-row bookkeeping this module owns.
        sub.completed_at = now

    if disposition in ("use_as_is", "rework_in_place"):
        session.flush()
        _recompute_step_status(session, ctx.step_exec, ctx.frozen_step, now=now)
    return {"status": sub.status, "disposition": disposition}


# -- photo (P3-12) --------------------------------------------------------------


def _execution_media_dir(unit_id: uuid.UUID) -> Path:
    base = Path(config.artifact_dir or "./artifacts") / "execution" / str(unit_id)
    base.mkdir(parents=True, exist_ok=True)
    return base


def attach_photo(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, data: bytes, ext: str, now: datetime | None = None,
) -> dict[str, Any]:
    """DD §9.5/§12.3: `<input capture>` upload (client resizes to <=2MP
    first, same static/library.js pattern as P2-06) -> stored under
    ARTIFACT_DIR/execution/{unit_id}/, linked to the substep execution."""
    now = now or datetime.now(timezone.utc)
    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.type != "photo":
        raise SubstepError("substep is not a photo type")

    sha = hashlib.sha256(data).hexdigest()
    name = f"{uuid.uuid4()}.{ext}"
    (_execution_media_dir(unit_id) / name).write_bytes(data)

    attachment = Attachment(
        entity_kind="substep_execution", entity_id=sub.id, kind="photo",
        path=f"execution/{unit_id}/{name}", sha256=sha, uploaded_by=operator.id, created_at=now,
    )
    session.add(attachment)
    session.flush()

    if sub.status not in ("done", "skipped"):
        if sub.started_at is None:
            sub.started_at = now
        sub.status = "done"
        sub.completed_at = now
        sub.operator_id = operator.id
        events.emit(
            session, "substep.done", entity=sub, actor_id=operator.id, station_id=station.id,
            after={"unit_id": str(unit_id), "attachment_id": str(attachment.id)},
        )
    session.flush()
    _recompute_step_status(session, ctx.step_exec, ctx.frozen_step, now=now)
    return {"status": sub.status, "attachment_id": str(attachment.id), "path": attachment.path}


# -- material (P3-12) ------------------------------------------------------------


def record_material(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, part_number: str, qty_used: str | float,
    qty_scrapped: str | float = 0, lot: str | None = None, uom: str | None = None,
    substitution_for: str | None = None, authorizer: Operator | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """§9.5/§12.3: material substep records qty used/scrapped vs plan.
    Substituting a different part than planned (`substitution_for` set)
    requires a lead-authorized second badge (`authorizer`)."""
    now = now or datetime.now(timezone.utc)
    if substitution_for and authorizer is None:
        raise SubstepError("material substitution requires lead authorization")

    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.type != "material":
        raise SubstepError("substep is not a material type")

    material = MaterialRecord(
        substep_execution_id=sub.id, unit_id=unit_id, plan_operation_id=ctx.plan_op.id,
        part_number=part_number, lot=lot, qty_used=Decimal(str(qty_used)),
        qty_scrapped=Decimal(str(qty_scrapped)), unit=uom, substitution_for=substitution_for,
        authorized_by=authorizer.id if authorizer else None, recorded_at=now,
    )
    session.add(material)
    session.flush()

    if sub.status not in ("done", "skipped"):
        if sub.started_at is None:
            sub.started_at = now
        sub.status = "done"
        sub.completed_at = now
        sub.operator_id = operator.id
        events.emit(
            session, "substep.done", entity=sub, actor_id=operator.id, station_id=station.id,
            after={
                "unit_id": str(unit_id), "material_record_id": str(material.id),
                "part_number": part_number,
            },
        )
    session.flush()
    _recompute_step_status(session, ctx.step_exec, ctx.frozen_step, now=now)
    return {"status": sub.status, "material_record_id": str(material.id)}


# -- signoff (P3-06) --------------------------------------------------------------


def complete_signoff(
    session: Session, *, station: Station, operator: Operator, unit_id: uuid.UUID,
    step_seq: int, substep_seq: int, authorizer: Operator, now: datetime | None = None,
) -> dict[str, Any]:
    """O8: signoff substep -- `authorizer` must already hold the substep's
    `signoff_role` (caller validates via deps.second_badge before calling,
    same split as scan.py's override_by / skip_substep's `lead`)."""
    now = now or datetime.now(timezone.utc)
    ctx = _resolve(
        session, station=station, operator=operator, unit_id=unit_id,
        step_seq=step_seq, substep_seq=substep_seq,
    )
    sub = ctx.sub_exec
    if sub.type != "signoff":
        raise SubstepError("substep is not a signoff type")
    if sub.status in ("done", "skipped"):
        return {"status": sub.status}

    if sub.started_at is None:
        sub.started_at = now
    sub.status = "done"
    sub.completed_at = now
    sub.operator_id = authorizer.id
    events.emit(
        session, "substep.done", entity=sub, actor_id=authorizer.id, station_id=station.id,
        after={
            "unit_id": str(unit_id), "signoff_role": ctx.frozen_sub.get("signoff_role"),
            "acting_operator_id": str(operator.id),
        },
    )
    session.flush()
    _recompute_step_status(session, ctx.step_exec, ctx.frozen_step, now=now)
    return {"status": "done"}
