"""Order ingestion -> WorkOrder + Units + frozen ExecutionPlan (P2-10, DD §4.3, §5, §6.1/6.2).

Entry point: `create_from_line_item(session, line_item)`, called from
`app/sync/hooks.py` on `order_line_item_new`/`order_line_item_changed`
(registered via `app.sync.worker.register_order_hook`).

Freeze invariant (DD §5 ExecutionPlan, task P2-10 AC): `plan_operations
.frozen_content` is a **full, independent copy** of the bound InstructionSet's
steps+substeps at the moment of binding -- never a reference. Publishing a
new version of that InstructionSet later must not change one byte of an
already-frozen `frozen_content`. `_freeze_instruction_set` below only reads
plain JSON-safe values (str/int/bool/dict/list) off the Step/Substep rows and
`copy.deepcopy`s the jsonb columns, so the result shares no mutable structure
with the live library rows.
"""
from __future__ import annotations

import copy
import logging
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.domain import conditions
from app.domain.binding import BindingResult, bind_full_routing
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_jb2 import JB2OrderLineItem, JB2OrderRouting, MappingException
from app.domain.models_library import InstructionSet, ProductPartMap, Step, Substep

logger = logging.getLogger("app.domain.workorders")

BLOCKED_PLACEHOLDER = {"blocked": True, "title": "no instructions — see lead"}

# Unit statuses that count as "not started" for cancel-vs-cancel_requested
# (DD §17.4, §4.3 rule 4) and for qty-decrease disposition (§17.4).
UNSTARTED_UNIT_STATUSES = {"queued"}


def _freeze_instruction_set(
    session: Session, iset: InstructionSet, variant_values: dict
) -> dict[str, Any]:
    """Full deep copy of `iset`'s steps+substeps, filtered by `condition`
    against `variant_values` (DD §7.2/§4.6) -- this is the freeze: nothing
    in the returned dict is shared with the live Step/Substep rows."""
    steps = session.scalars(
        select(Step).where(Step.instruction_set_id == iset.id).order_by(Step.seq)
    ).all()
    frozen_steps = []
    for step in steps:
        substeps = session.scalars(
            select(Substep).where(Substep.step_id == step.id).order_by(Substep.seq)
        ).all()
        frozen_substeps = [
            {
                "seq": sub.seq,
                "type": sub.type,
                "title": sub.title,
                "body_html": sub.body_html,
                "required": sub.required,
                "measurement_spec": copy.deepcopy(sub.measurement_spec),
                "media": copy.deepcopy(sub.media),
                "signoff_role": sub.signoff_role,
            }
            for sub in substeps
            if conditions.evaluate(sub.condition, variant_values)
        ]
        frozen_steps.append(
            {
                "seq": step.seq,
                "title": step.title,
                "body_html": step.body_html,
                "est_minutes": step.est_minutes,
                "substeps": frozen_substeps,
            }
        )
    return {
        "instruction_set_id": str(iset.id),
        "version": iset.version,
        "steps": frozen_steps,
    }


def _build_plan_operation(
    session: Session,
    work_order: WorkOrder,
    routing_step: JB2OrderRouting,
    bind_result: BindingResult,
    variant_values: dict,
) -> PlanOperation:
    if bind_result.blocked or bind_result.instruction_set is None:
        return PlanOperation(
            id=uuid.uuid4(),
            work_order_id=work_order.id,
            seq=routing_step.seq,
            jb2_routing_id=routing_step.id,
            operation_code=routing_step.operation_code,
            title=routing_step.description or routing_step.operation_code or "Unbound operation",
            instruction_set_id=None,
            instruction_version=None,
            frozen_content=copy.deepcopy(BLOCKED_PLACEHOLDER),
            status="pending",
            est_minutes=None,
            blocked=True,
        )

    iset = bind_result.instruction_set
    frozen_content = _freeze_instruction_set(session, iset, variant_values)
    return PlanOperation(
        id=uuid.uuid4(),
        work_order_id=work_order.id,
        seq=routing_step.seq,
        jb2_routing_id=routing_step.id,
        operation_code=routing_step.operation_code,
        title=iset.title,
        instruction_set_id=iset.id,
        instruction_version=iset.version,
        frozen_content=frozen_content,
        status="pending",
        est_minutes=iset.est_minutes,
        blocked=False,
    )


def _generate_plan_operations(
    session: Session, work_order: WorkOrder, routings: list[JB2OrderRouting]
) -> bool:
    """Bind `routings` and create/replace `work_order`'s plan_operations.
    Returns True if any step is unbound (blocked)."""
    binding_summary = bind_full_routing(session, work_order.product_id, routings)
    variant_values = work_order.variant_values or {}
    for routing_step, bind_result in zip(routings, binding_summary.results):
        plan_op = _build_plan_operation(
            session, work_order, routing_step, bind_result, variant_values
        )
        session.add(plan_op)
    return binding_summary.any_blocked


def _line_item_routings(session: Session, line_item_id: uuid.UUID) -> list[JB2OrderRouting]:
    return session.scalars(
        select(JB2OrderRouting)
        .where(JB2OrderRouting.jb2_line_item_id == line_item_id)
        .order_by(JB2OrderRouting.seq)
    ).all()


def create_from_line_item(
    session: Session, line_item: JB2OrderLineItem
) -> WorkOrder | None:
    """DD §4.3 rules 1-2. Idempotent: a line item that already has a
    WorkOrder delegates to `reconcile_changes` instead of re-creating one.

    Returns the WorkOrder, or None if the part number has no product
    mapping (a `mapping_exceptions` row is recorded instead -- DD §4.6:
    unmapped values never crash a sync, they're an admin to-do)."""
    existing = session.scalars(
        select(WorkOrder).where(WorkOrder.jb2_line_item_id == line_item.id)
    ).one_or_none()
    if existing is not None:
        reconcile_changes(session, existing, line_item)
        return existing

    part_map = None
    if line_item.part_number:
        part_map = session.scalars(
            select(ProductPartMap).where(
                ProductPartMap.jb2_part_number == line_item.part_number
            )
        ).one_or_none()

    if part_map is None:
        _record_unmapped_part(session, line_item)
        return None

    variant_values = part_map.variant_values or {}
    qty = int(line_item.qty) if line_item.qty is not None else 1

    work_order = WorkOrder(
        id=uuid.uuid4(),
        jb2_line_item_id=line_item.id,
        product_id=part_map.product_id,
        variant_values=variant_values,
        qty=qty,
        due_date=line_item.due_date,
        status="pending_sync",
        plan_version=1,
        priority=0,
    )
    session.add(work_order)
    session.flush()  # work_order.id needed by plan_operations/units FKs below

    routings = _line_item_routings(session, line_item.id)
    any_blocked = _generate_plan_operations(session, work_order, routings)
    work_order.status = "blocked_no_instructions" if any_blocked else "ready"

    for unit_no in range(1, qty + 1):
        session.add(
            Unit(
                id=uuid.uuid4(),
                work_order_id=work_order.id,
                unit_no=unit_no,
                serial_number=None,  # CR-007: serial entered at kit-up, never generated here
                status="queued",
                first_pass=True,
                rework_count=0,
            )
        )

    logger.info(
        "workorder_created",
        extra={
            "work_order_id": str(work_order.id),
            "jb2_line_item_id": str(line_item.id),
            "qty": qty,
            "status": work_order.status,
            "any_blocked": any_blocked,
        },
    )

    # FLAGGED touch (P2-11, DD §8): auto-generate the v1 build guide PDF for
    # non-blocked work orders at creation time. A blocked_no_instructions WO
    # gets no auto-PDF -- the manual /admin/work-orders/{id}/generate-pdf
    # endpoint (app/api/workorders.py) covers that case with the placeholder
    # page instead. Local import to keep app.domain free of a module-level
    # dependency on app.pdf.
    if not any_blocked:
        from app.pdf.guide import generate_build_guide

        generate_build_guide(session, work_order, generated_by="system")

    return work_order


def _record_unmapped_part(session: Session, line_item: JB2OrderLineItem) -> None:
    value = line_item.part_number or ""
    already_flagged = session.scalars(
        select(MappingException).where(
            MappingException.kind == "part_number",
            MappingException.value == value,
            MappingException.resolved.is_(False),
        )
    ).first()
    if already_flagged is not None:
        return  # ponytail: don't spam a new row every sync cycle for the same unmapped part
    session.add(
        MappingException(
            id=uuid.uuid4(),
            kind="part_number",
            value=value,
            context={"jb2_line_item_id": str(line_item.id), "jb2_id": line_item.jb2_id},
            resolved=False,
        )
    )
    logger.warning(
        "workorder_unmapped_part",
        extra={"jb2_line_item_id": str(line_item.id), "part_number": value},
    )


# --------------------------------------------------------------------------
# Reconciliation (DD §17.4/17.5) -- minimal per the P2-10 brief; the lead
# disposition workflow (picking which unstarted units to cancel, regenerate
# vs. ignore routing drift) is Phase 3 (P2-13 tracks the fuller version).
# --------------------------------------------------------------------------


def reconcile_changes(
    session: Session, work_order: WorkOrder, line_item: JB2OrderLineItem
) -> WorkOrder:
    """Order-changed reconciliation for an existing WorkOrder. Never mutates
    a frozen plan_operation's `frozen_content` -- routing drift is flagged,
    not auto-applied (§17.5: "never silently mutated")."""
    changes: list[str] = []

    if line_item.due_date != work_order.due_date:
        work_order.due_date = line_item.due_date
        changes.append("due_date")

    _reconcile_qty(session, work_order, line_item, changes)
    _reconcile_routing_drift(session, work_order, line_item, changes)

    if changes:
        logger.info(
            "workorder_reconcile",
            extra={"work_order_id": str(work_order.id), "changes": changes},
        )
    return work_order


def _reconcile_qty(
    session: Session, work_order: WorkOrder, line_item: JB2OrderLineItem, changes: list[str]
) -> None:
    if line_item.qty is None:
        return
    new_qty = int(line_item.qty)
    if new_qty == work_order.qty:
        return

    if new_qty > work_order.qty:
        existing_units = session.scalars(
            select(Unit).where(Unit.work_order_id == work_order.id)
        ).all()
        max_unit_no = max((u.unit_no for u in existing_units), default=0)
        for unit_no in range(max_unit_no + 1, max_unit_no + 1 + (new_qty - work_order.qty)):
            session.add(
                Unit(
                    id=uuid.uuid4(),
                    work_order_id=work_order.id,
                    unit_no=unit_no,
                    serial_number=None,
                    status="queued",
                    first_pass=True,
                    rework_count=0,
                )
            )
        changes.append(f"qty_increase:{work_order.qty}->{new_qty}")
    else:
        note = (
            f"JB2 qty decreased {work_order.qty} -> {new_qty}; "
            "lead must pick unstarted units to cancel (DD §17.4)."
        )
        work_order.notes = f"{work_order.notes}\n{note}" if work_order.notes else note
        changes.append(f"qty_decrease:{work_order.qty}->{new_qty}")

    work_order.qty = new_qty


def _reconcile_routing_drift(
    session: Session, work_order: WorkOrder, line_item: JB2OrderLineItem, changes: list[str]
) -> None:
    if work_order.status in ("cancelled", "cancel_requested"):
        return  # a cancelled/cancel_requested WO's plan is moot -- don't drift-flag it

    routings = _line_item_routings(session, line_item.id)
    plan_ops = session.scalars(
        select(PlanOperation)
        .where(PlanOperation.work_order_id == work_order.id)
        .order_by(PlanOperation.seq)
    ).all()
    current_sig = [(r.seq, r.operation_code) for r in routings]
    frozen_sig = [(p.seq, p.operation_code) for p in plan_ops]
    if current_sig != frozen_sig and work_order.status != "routing_drift":
        work_order.status = "routing_drift"
        changes.append("routing_drift")


def cancel_work_order(session: Session, work_order: WorkOrder) -> WorkOrder:
    """DD §4.3 rule 4 / §17.4: order canceled/closed in JB2 -> `cancelled` if
    no unit has left `queued`, else `cancel_requested` for a lead to
    disposition."""
    units = session.scalars(select(Unit).where(Unit.work_order_id == work_order.id)).all()
    any_started = any(u.status not in UNSTARTED_UNIT_STATUSES for u in units)
    work_order.status = "cancel_requested" if any_started else "cancelled"
    logger.info(
        "workorder_cancelled",
        extra={
            "work_order_id": str(work_order.id),
            "status": work_order.status,
            "any_started": any_started,
        },
    )
    return work_order


def regenerate_plan(session: Session, work_order: WorkOrder) -> WorkOrder:
    """Rebind + refreeze `work_order`'s plan_operations from the current
    published library state and current JB2 routing, bumping
    `plan_version`. Used by the (Phase 3) lead reconciliation flow for
    routing_drift / qty-change disposition; simple/whole-plan for now."""
    session.execute(delete(PlanOperation).where(PlanOperation.work_order_id == work_order.id))
    session.flush()

    routings = _line_item_routings(session, work_order.jb2_line_item_id)
    any_blocked = _generate_plan_operations(session, work_order, routings)
    work_order.plan_version += 1
    work_order.status = "blocked_no_instructions" if any_blocked else "ready"
    logger.info(
        "workorder_plan_regenerated",
        extra={
            "work_order_id": str(work_order.id),
            "plan_version": work_order.plan_version,
            "any_blocked": any_blocked,
        },
    )
    return work_order
