"""Kit-up, box lifecycle, and box reassignment (P3-11, DD §6.1.5/§17.6,
docs/state-machine.md §7 (box reassignment) and §8 (kit-up)).

CR-007: serials **pre-exist** in Atlas's system -- the operator ENTERS an
existing serial at kit-up (or later via `set_serial`); this module never
generates one.

Every mutating function here is meant to be called from an API layer that
has already resolved the acting operator (badge lookup / role gate) -- same
convention as `app.domain.statemachine`, which takes an already-resolved
`Operator`/`override_by` rather than re-deriving identity from a request.
Role gating (e.g. "reassign requires a lead", `KITUP_REQUIRES_LEAD`) happens
in `app/api/boxes.py`, not here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import events
from app.domain.models_execution import Unit, WorkOrder
from app.domain.models_floor import BoxAssignment, BuildBox, Operator

WORK_ORDER_KITUP_STATUSES = ("ready", "in_progress")


class BoxError(ValueError):
    """Friendly, user-facing kit-up/box error -- callers render `str(exc)`
    directly (same pattern as `app.auth.service`'s `AuthError` family)."""


def register_box(session: Session, qr_payload: str, label: str | None = None) -> BuildBox:
    """Get-or-create by `qr_payload` -- idempotent (DD §6.1.5: a box may be
    scanned/typed at kit-up before it has ever been persisted, e.g. a fresh
    label straight off the print sheet, or CR-009's manual-entry fallback
    re-submitting the same payload). Never overwrites an existing row's
    `label` -- that was fixed once at print/first-registration time."""
    box = session.execute(
        select(BuildBox).where(BuildBox.qr_payload == qr_payload)
    ).scalar_one_or_none()
    if box is not None:
        return box
    box = BuildBox(qr_payload=qr_payload, label=label, active=True)
    session.add(box)
    session.flush()
    return box


def _unit_current_box(session: Session, unit_id) -> BuildBox | None:
    return session.execute(
        select(BuildBox).where(BuildBox.current_unit_id == unit_id)
    ).scalar_one_or_none()


def boxless_units(session: Session, work_order_id) -> list[Unit]:
    """Units on `work_order_id` with no `BuildBox.current_unit_id` pointing
    at them and not yet terminal -- the kit-up unit picker's candidate list."""
    bound_ids = select(BuildBox.current_unit_id).where(BuildBox.current_unit_id.is_not(None))
    return list(
        session.execute(
            select(Unit)
            .where(
                Unit.work_order_id == work_order_id,
                Unit.status.not_in(("done", "scrapped")),
                Unit.id.not_in(bound_ids),
            )
            .order_by(Unit.unit_no)
        ).scalars()
    )


def work_orders_needing_kitup(session: Session) -> list[WorkOrder]:
    """WorkOrders ready/in_progress that have at least one boxless unit --
    contract §8's kit-up precondition, at the WO granularity for the picker."""
    candidates = session.execute(
        select(WorkOrder)
        .where(WorkOrder.status.in_(WORK_ORDER_KITUP_STATUSES))
        .order_by(WorkOrder.created_at.desc())
    ).scalars()
    return [wo for wo in candidates if boxless_units(session, wo.id)]


def _check_serial_unique(session: Session, serial: str, *, exclude_unit_id) -> None:
    # migrations/0006's `uq_units_serial_number_when_set` is the DB-level
    # backstop; this is the friendly, pre-flight version of the same rule
    # (a bare IntegrityError has no good message for a floor tablet).
    existing = session.execute(
        select(Unit).where(Unit.serial_number == serial, Unit.id != exclude_unit_id)
    ).scalars().first()
    if existing is not None:
        raise BoxError(f"serial {serial!r} is already recorded on another unit")


def assign_box(
    session: Session,
    box: BuildBox,
    unit: Unit,
    assigned_by: Operator,
    serial: str | None = None,
    *,
    now: datetime | None = None,
) -> BoxAssignment:
    """Contract §8: binds `box` to `unit`.

    Preconditions (raise `BoxError`, friendly message, no mutation):
      - unit's work order is `ready` or `in_progress`
      - box is active and unassigned (`current_unit_id is None`)
      - unit doesn't already have a box
      - `serial`, if given, isn't already recorded on another unit

    Effects: `box_assignments` row; `box.current_unit_id` set; `unit
    .serial_number` set if `serial` given; work order -> `in_progress` on
    first-ever kit-up (`ready` -> `in_progress`, contract §8); events:
    `box.assigned` (+ `wo.status_changed` iff the WO flipped).
    """
    now = now or datetime.now(timezone.utc)

    work_order = session.get(WorkOrder, unit.work_order_id)
    if work_order is None or work_order.status not in WORK_ORDER_KITUP_STATUSES:
        raise BoxError("work order must be ready or in progress to kit up a unit")
    if not box.active:
        raise BoxError("this box is retired -- register or select a different one")
    if box.current_unit_id is not None:
        raise BoxError("box is already assigned to a unit -- release or reassign it first")
    if _unit_current_box(session, unit.id) is not None:
        raise BoxError("unit already has a box assigned")
    if serial:
        _check_serial_unique(session, serial, exclude_unit_id=unit.id)

    box.current_unit_id = unit.id
    box.last_scan_at = now

    assignment = BoxAssignment(
        box_id=box.id, unit_id=unit.id, assigned_by=assigned_by.display_name, assigned_at=now,
    )
    session.add(assignment)
    if serial:
        unit.serial_number = serial
    session.flush()

    events.emit(
        session, "box.assigned", entity=assignment, actor_id=assigned_by.id,
        after={
            "box_id": str(box.id), "unit_id": str(unit.id), "work_order_id": str(work_order.id),
            "serial": serial,
        },
    )

    if work_order.status == "ready":
        events.emit(
            session, "wo.status_changed", entity=work_order, actor_id=assigned_by.id,
            before={"status": "ready"}, after={"status": "in_progress"},
        )
        work_order.status = "in_progress"

    return assignment


def release_box(session: Session, box: BuildBox, released_by: Operator | None = None) -> BuildBox:
    """Detaches `box` from its current unit: closes the open
    `box_assignments` row (`released_at`) and clears `box.current_unit_id`.
    No-op-safe: raises `BoxError` if the box isn't currently assigned (callers
    that just want "make sure it's free" should check `box.current_unit_id`
    first rather than relying on this being idempotent)."""
    if box.current_unit_id is None:
        raise BoxError("box is not currently assigned")

    unit_id = box.current_unit_id
    open_assignment = session.execute(
        select(BoxAssignment)
        .where(
            BoxAssignment.box_id == box.id, BoxAssignment.unit_id == unit_id,
            BoxAssignment.released_at.is_(None),
        )
        .order_by(BoxAssignment.assigned_at.desc())
    ).scalars().first()

    now = datetime.now(timezone.utc)
    if open_assignment is not None:
        open_assignment.released_at = now
    box.current_unit_id = None

    events.emit(
        session, "box.released", entity=(open_assignment or box),
        actor_id=released_by.id if released_by else None,
        after={"box_id": str(box.id), "unit_id": str(unit_id)},
    )
    session.flush()
    return box


def reassign_box(
    session: Session,
    old_box: BuildBox,
    new_box: BuildBox,
    unit: Unit,
    lead: Operator,
    retire_old: bool = False,
) -> BoxAssignment:
    """§17.6 lost/damaged box: releases `old_box` (history preserved as a
    closed `box_assignments` row) and binds `new_box` to the same `unit`,
    carrying its existing serial forward untouched. Optionally retires
    `old_box` (`active=False`) so it never gets handed out again."""
    if old_box.current_unit_id != unit.id:
        raise BoxError("old box is not currently assigned to this unit")

    release_box(session, old_box, released_by=lead)
    if retire_old:
        old_box.active = False
    new_assignment = assign_box(session, new_box, unit, lead)

    # Distinct from the box.assigned/box.released pair above: this is the
    # unit's own timeline entry (events.timeline(unit_id=...) only matches
    # rows whose entity IS the unit -- box.assigned/released are filed
    # against the BoxAssignment/box, so without this a unit's history would
    # have a gap across a box swap).
    events.emit(
        session, "unit.moved", entity=unit, actor_id=lead.id,
        after={
            "reason": "box_reassigned", "old_box_id": str(old_box.id),
            "new_box_id": str(new_box.id), "retired_old": retire_old,
        },
    )
    return new_assignment


def set_serial(session: Session, unit: Unit, serial: str, by: Operator | None = None) -> Unit:
    """Records an operator-entered serial on `unit` outside the kit-up flow
    (e.g. a correction, or entry deferred past kit-up). CR-007: never
    generates a serial, only validates and stores one that's handed to it.

    `by` isn't persisted anywhere yet -- no event verb in
    docs/state-machine.md §9 is reserved for a standalone serial correction
    (only the kit-up's `box.assigned` and finish's serial-required gate are
    in the contract); kept as a parameter for the caller/future audit hook
    rather than silently dropped.
    """
    if unit.status == "done":
        raise BoxError("cannot set a serial on a completed unit")
    if not serial:
        raise BoxError("serial is required")
    _check_serial_unique(session, serial, exclude_unit_id=unit.id)
    unit.serial_number = serial
    session.flush()
    return unit
