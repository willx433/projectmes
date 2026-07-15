"""Dashboard state derivation (P4-02, DD §13.1, CR-004, pistol_flow_visual
§4). Pure(ish) read-only functions over the existing Execution/Floor tables
-- no new columns, no new table. One function computes a single unit's
dashboard state; one builds its full card dict; one aggregates the KPI
strip; one groups cards for the board's grouping toggle.

CR-004 union: 7 mutually exclusive card states (`STATES` below). Precedence
(highest wins) when more than one condition applies to the same unit --
this ordering is this task's own call, not spelled out in the DD/visual:
blocked_no_instructions > overdue > stalled > rework > in_transit > queued
> first_pass. Rationale: a missing instruction set stops the unit outright;
a missed customer due date outranks an internal dwell alert; a dwell alert
outranks the first-pass/rework distinction (either kind of unit can stall);
rework outranks the plain in_transit/queued labels since "this unit needed
rework" is the more useful signal once nothing red/amber applies.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    BuildBox,
    Failure,
    Operator,
    StepExecution,
    Transit,
    WorkSession,
)
from app.domain.models_jb2 import JB2OrderLineItem
from app.domain.models_library import FailureCode, Product
from app.domain.statemachine import unit_next_op

# CR-004: union of DD §13.1's state list and pistol_flow_visual §4's card set.
STATES = (
    "blocked_no_instructions",
    "overdue",
    "stalled",
    "rework",
    "in_transit",
    "queued",
    "first_pass",
)

STATE_COLOR = {
    "blocked_no_instructions": "hatched",
    "overdue": "red",
    "stalled": "amber",
    "rework": "purple",
    "in_transit": "green",
    "queued": "grey",
    "first_pass": "green",
}

# Units still "in the flow" for the board -- done/scrapped units leave it
# (pistol_flow_visual §1: "Unit -> done; card leaves the board").
ACTIVE_UNIT_STATUSES = ("queued", "at_station", "in_transit")


def _naive_utc(dt: datetime) -> datetime:
    """Sqlite round-trips DateTime columns as naive even when a tz-aware
    value was written; Postgres keeps tzinfo. Normalize both sides to naive
    UTC before subtracting -- same pattern as
    app/domain/statemachine.py's `_close_open_transit`."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _hours_since(now: datetime, then: datetime) -> float:
    return (_naive_utc(now) - _naive_utc(then)).total_seconds() / 3600.0


def _open_work_session(session: Session, unit: Unit) -> WorkSession | None:
    return session.execute(
        select(WorkSession)
        .where(WorkSession.unit_id == unit.id, WorkSession.ended_at.is_(None))
        .order_by(WorkSession.started_at.desc())
    ).scalars().first()


def _last_event_at(session: Session, unit: Unit) -> datetime | None:
    """Most recent timestamp we have for "something happened to this unit"
    when no session is currently open -- the reference point for queue-dwell.
    A unit that has never moved or been worked (freshly queued, never
    scanned) has no such reference and is never considered stalled by this
    function; it just reads as plain `queued`."""
    candidates: list[datetime] = []
    last_transit = session.execute(
        select(Transit).where(Transit.unit_id == unit.id).order_by(Transit.departed_at.desc())
    ).scalars().first()
    if last_transit is not None:
        candidates.append(last_transit.arrived_at or last_transit.departed_at)
    last_closed_ws = session.execute(
        select(WorkSession)
        .where(WorkSession.unit_id == unit.id, WorkSession.ended_at.is_not(None))
        .order_by(WorkSession.ended_at.desc())
    ).scalars().first()
    if last_closed_ws is not None:
        candidates.append(last_closed_ws.ended_at)
    return max(candidates) if candidates else None


def _is_stalled(session: Session, unit: Unit, now: datetime, cfg) -> tuple[bool, float | None]:
    """Returns (stalled?, dwell_hours) -- see app/config.py's docstring for
    the two-threshold rationale."""
    open_ws = _open_work_session(session, unit)
    if open_ws is not None:
        hours = _hours_since(now, open_ws.started_at)
        return hours > cfg.dashboard_stalled_hrs, hours
    since = _last_event_at(session, unit)
    if since is None:
        return False, None
    hours = _hours_since(now, since)
    return hours > cfg.dashboard_queue_dwell_hrs, hours


def compute_unit_state(
    session: Session,
    unit: Unit,
    work_order: WorkOrder | None,
    current_op: PlanOperation | None,
    now: datetime,
    cfg,
) -> tuple[str, float | None]:
    """Returns (state, dwell_hours). `dwell_hours` is only meaningful for the
    `stalled` state but is returned regardless -- callers may want it for
    display even off the state's own precedence rung."""
    stalled, dwell_hours = _is_stalled(session, unit, now, cfg)

    if (current_op is not None and current_op.blocked) or (
        work_order is not None and work_order.status == "blocked_no_instructions"
    ):
        return "blocked_no_instructions", dwell_hours
    if (
        work_order is not None
        and work_order.due_date is not None
        and work_order.due_date < now.date()
    ):
        return "overdue", dwell_hours
    if stalled:
        return "stalled", dwell_hours
    if not unit.first_pass:
        return "rework", dwell_hours
    if unit.status == "in_transit":
        return "in_transit", dwell_hours
    if unit.status == "queued":
        return "queued", dwell_hours
    return "first_pass", dwell_hours


def _redo_start_seq(session: Session, unit: Unit, current_seq: int) -> int:
    """Earliest op seq this rework unit was sent back to -- drives the
    `redo` (purple) segment range in the op-progress track. Falls back to
    `current_seq` (only the current segment reads as redo) if there's no
    Failure row to derive it from (e.g. a hand-seeded test unit)."""
    failures = session.scalars(
        select(Failure).where(Failure.unit_id == unit.id)
    ).all()
    seqs = []
    for f in failures:
        target_id = f.rework_to_op if f.disposition == "rework_to_op" else f.plan_operation_id
        target = session.get(PlanOperation, target_id) if target_id else None
        if target is not None:
            seqs.append(target.seq)
    return min(seqs) if seqs else current_seq


def _op_segments(
    session: Session, unit: Unit, all_ops: list[PlanOperation],
    current_op: PlanOperation | None,
) -> list[dict]:
    if current_op is None:
        # unit has run off the end of its route (shouldn't happen for an
        # "active" unit, but degrade gracefully rather than crash).
        return [{"seq": op.seq, "cls": "done"} for op in all_ops]

    redo_start = _redo_start_seq(session, unit, current_op.seq) if not unit.first_pass else None
    segments = []
    for op in all_ops:
        if redo_start is not None and redo_start <= op.seq <= current_op.seq:
            cls = "redo"
        elif op.seq < current_op.seq:
            cls = "done"
        elif op.seq == current_op.seq:
            cls = "cur"
        else:
            cls = ""
        segments.append({"seq": op.seq, "title": op.title, "cls": cls})
    return segments


def _route_pct(
    session: Session, unit: Unit, all_ops: list[PlanOperation],
    current_op: PlanOperation | None,
) -> tuple[int, int, int]:
    """Returns (route_pct, done_step_count, total_step_count_this_op)."""
    if not all_ops or current_op is None:
        return 100, 0, 0
    completed_before = sum(1 for op in all_ops if op.seq < current_op.seq)
    steps = (current_op.frozen_content or {}).get("steps") or []
    total_steps = len(steps)
    if total_steps == 0:
        fraction = 0.0
        done_steps = 0
    else:
        done_steps = session.execute(
            select(StepExecution)
            .where(
                StepExecution.unit_id == unit.id,
                StepExecution.plan_operation_id == current_op.id,
                StepExecution.status == "done",
                StepExecution.superseded.is_(False),
            )
        ).scalars().all()
        done_steps = len({s.step_seq for s in done_steps})
        fraction = done_steps / total_steps
    pct = round((completed_before + fraction) / len(all_ops) * 100)
    return pct, done_steps, total_steps


def _job_ref(session: Session, work_order: WorkOrder | None) -> str | None:
    if work_order is None:
        return None
    line_item = session.get(JB2OrderLineItem, work_order.jb2_line_item_id)
    if line_item is None:
        return None
    return (line_item.payload or {}).get("jobNumber")


def _current_box(session: Session, unit: Unit) -> BuildBox | None:
    return session.execute(
        select(BuildBox).where(BuildBox.current_unit_id == unit.id)
    ).scalar_one_or_none()


def _last_failure(session: Session, unit: Unit) -> Failure | None:
    return session.execute(
        select(Failure).where(Failure.unit_id == unit.id).order_by(Failure.detected_at.desc())
    ).scalars().first()


def build_card(session: Session, unit: Unit, now: datetime, cfg) -> dict:
    """The full JSON card for one active unit -- everything
    templates/dashboard/_card.html renders from, and what
    GET /api/v1/dashboard/pipeline returns per unit."""
    work_order = session.get(WorkOrder, unit.work_order_id)
    product = session.get(Product, work_order.product_id) if work_order else None
    current_op = unit_next_op(session, unit)
    all_ops = session.scalars(
        select(PlanOperation)
        .where(PlanOperation.work_order_id == unit.work_order_id, PlanOperation.status != "skipped")
        .order_by(PlanOperation.seq)
    ).all()

    state, dwell_hours = compute_unit_state(session, unit, work_order, current_op, now, cfg)
    route_pct, done_steps, total_steps = _route_pct(session, unit, all_ops, current_op)
    box = _current_box(session, unit)

    operator_name = None
    elapsed_seconds = None
    if unit.status == "at_station":
        ws = _open_work_session(session, unit)
        if ws is not None:
            operator = session.get(Operator, ws.operator_id)
            operator_name = operator.display_name if operator else None
            elapsed_seconds = int(_hours_since(now, ws.started_at) * 3600)

    station_label = None
    if current_op is not None:
        if state == "blocked_no_instructions":
            station_label = f"Blocked — {current_op.title} (no instructions)"
        elif unit.status == "in_transit":
            station_label = f"In transit → {current_op.title}"
        elif unit.status == "queued":
            station_label = f"Queued at {current_op.title}"
        else:
            station_label = current_op.title

    last_failure_label = None
    if state in ("rework", "blocked_no_instructions") or not unit.first_pass:
        failure = _last_failure(session, unit)
        if failure is not None:
            code = session.get(FailureCode, failure.failure_code_id)
            last_failure_label = f"{code.code} {code.label}" if code else failure.narrative

    overdue_days = None
    if work_order is not None and work_order.due_date is not None:
        overdue_days = (now.date() - work_order.due_date).days
        if overdue_days <= 0:
            overdue_days = None

    return {
        "unit_id": str(unit.id),
        "work_order_id": str(unit.work_order_id),
        "product": product.name if product else None,
        "variant": work_order.variant_values if work_order else None,
        "serial": unit.serial_number,
        "unit_no": unit.unit_no,
        "job_ref": _job_ref(session, work_order),
        "box_label": (box.label or box.qr_payload) if box is not None else None,
        "station_label": station_label,
        "operation_title": current_op.title if current_op else None,
        "operator": operator_name,
        "elapsed_seconds": elapsed_seconds,
        "ops": _op_segments(session, unit, all_ops, current_op),
        "route_pct": route_pct,
        "step_progress": f"{done_steps}/{total_steps}" if total_steps else None,
        "due_date": work_order.due_date.isoformat() if work_order and work_order.due_date else None,
        "overdue_days": overdue_days,
        "dwell_hours": round(dwell_hours, 1) if dwell_hours is not None else None,
        "first_pass": unit.first_pass,
        "rework_count": unit.rework_count,
        "unit_status": unit.status,
        "state": state,
        "color": STATE_COLOR[state],
        "badge_label": (
            "Awaiting start" if state == "queued"
            else (f"Rework ×{unit.rework_count}" if not unit.first_pass else "First pass")
        ),
        "last_failure": last_failure_label,
    }


def active_units(session: Session) -> list[Unit]:
    return list(
        session.scalars(
            select(Unit).where(Unit.status.in_(ACTIVE_UNIT_STATUSES))
        ).all()
    )


def build_board(session: Session, now: datetime | None, cfg) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    return [build_card(session, unit, now, cfg) for unit in active_units(session)]


def compute_kpis(cards: list[dict]) -> dict:
    wip = len(cards)
    first_pass_n = sum(1 for c in cards if c["first_pass"])
    on_time_n = sum(1 for c in cards if c["state"] != "overdue")
    return {
        "wip": wip,
        "first_pass_pct": round(100 * first_pass_n / wip) if wip else 0,
        "in_rework": sum(1 for c in cards if not c["first_pass"]),
        "stalled": sum(1 for c in cards if c["state"] == "stalled"),
        "on_time_pct": round(100 * on_time_n / wip) if wip else 100,
    }


GROUP_BY_CHOICES = ("station", "product", "due")


def group_cards(cards: list[dict], group_by: str | None) -> list[dict]:
    """Returns an ordered list of {"key": ..., "cards": [...]}. `group_by`
    None (or unrecognized) yields a single ungrouped group."""
    if group_by not in GROUP_BY_CHOICES:
        return [{"key": None, "cards": cards}]

    def key_fn(card: dict) -> str:
        if group_by == "station":
            return card["station_label"] or "—"
        if group_by == "product":
            return card["product"] or "—"
        return card["due_date"] or "—"

    buckets: dict[str, list[dict]] = defaultdict(list)
    for card in cards:
        buckets[key_fn(card)].append(card)
    return [{"key": key, "cards": buckets[key]} for key in sorted(buckets)]
