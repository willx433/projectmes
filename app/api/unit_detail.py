"""Unit drill-down (P4-03, DD §13.1 click-through + §9.1-9.4 data catalog).

`GET /units/{unit_id}` renders the full chronological build record for one
physical unit: every scan, box assignment, work session (with pauses),
step/substep execution, measurement, failure/disposition, scrap event, and
transit -- plus the unit-level audit events (`events.timeline`) that narrate
transitions the detail tables don't fully explain on their own (box
reassignment's "why", rework mode, scrap linkage). `GET
/api/v1/units/{unit_id}/timeline` returns the same payload as JSON.

Design note on sourcing: `events.timeline(unit_id=...)` (app/domain/events.py)
only matches Event rows whose entity IS the unit itself (`unit.moved`,
`unit.reworked`, `unit.scrapped`, `unit.done`) -- per that function's own
docstring it does NOT reach into scan/session/substep/measurement/failure
rows that merely reference the unit. Those come from direct queries against
their own tables here, keyed by unit_id (or, for scans, by the box(es) ever
assigned to this unit during their assigned/released window -- a scan only
carries `box_id`, not `unit_id`, so box_assignments is the join path DD's
schema actually provides, see state-machine.md's box-recycling note).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.db import get_session
from app.domain import events as events_domain
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    BoxAssignment,
    BuildBox,
    Failure,
    Measurement,
    Operator,
    Scan,
    ScrapEvent,
    SessionPause,
    Station,
    StepExecution,
    SubstepExecution,
    Transit,
    WorkSession,
)
from app.domain.models_jb2 import JB2OrderLineItem
from app.domain.models_library import FailureCode, Product

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))


def _naive(dt: datetime) -> datetime:
    # ponytail: same sqlite-naive/postgres-aware normalization used across
    # app/domain/statemachine.py and app/domain/sessions.py -- needed here
    # purely as a sort key so entries from columns that round-trip naive on
    # sqlite still interleave correctly with any aware ones.
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, Decimal) else value


def _frozen_titles(
    plan_op: PlanOperation | None, step_seq: int, sub_seq: int
) -> tuple[str | None, str | None]:
    """Lenient (unit_id, step_seq, substep_seq) -> (step_title, substep_title)
    lookup against the frozen plan content -- unlike
    app.domain.substeps._frozen_step/_frozen_substep this never raises; a
    miss just renders without a title rather than 500ing a drill-down page."""
    if plan_op is None:
        return None, None
    step_title = None
    sub_title = None
    for step in (plan_op.frozen_content or {}).get("steps", []):
        if step.get("seq") == step_seq:
            step_title = step.get("title")
            for sub in step.get("substeps", []):
                if sub.get("seq") == sub_seq:
                    sub_title = sub.get("title")
            break
    return step_title, sub_title


# kind -> CSS/rendering category, matched by prefix (order matters: checked
# top to bottom) -- lets the template color-code entries without repeating
# this string logic in Jinja.
_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("box_", "box"),
    ("scan", "scan"),
    ("session_", "session"),
    ("substep_", "substep"),
    ("measurement", "measurement"),
    ("failure", "failure"),
    ("scrap", "scrap"),
    ("transit_", "transit"),
    ("event_", "event"),
)


def _category(kind: str) -> str:
    for prefix, cat in _CATEGORY_PREFIXES:
        if kind.startswith(prefix):
            return cat
    return "other"


def build_unit_timeline(session: Session, unit_id: uuid.UUID) -> dict[str, Any] | None:
    """Assembles the full §9.1-9.4 record for one unit. Returns None if the
    unit doesn't exist (caller maps that to a 404)."""
    unit = session.get(Unit, unit_id)
    if unit is None:
        return None

    work_order = session.get(WorkOrder, unit.work_order_id)
    product = session.get(Product, work_order.product_id) if work_order else None
    line_item = (
        session.get(JB2OrderLineItem, work_order.jb2_line_item_id) if work_order else None
    )

    plan_ops = list(
        session.scalars(
            select(PlanOperation)
            .where(PlanOperation.work_order_id == unit.work_order_id)
            .order_by(PlanOperation.seq)
        )
    )
    plan_op_by_id = {op.id: op for op in plan_ops}

    # Reused across entries below -- small tables, cheap to preload wholesale
    # rather than resolving one id at a time.
    operators_by_id = {o.id: o for o in session.scalars(select(Operator))}
    stations_by_id = {s.id: s for s in session.scalars(select(Station))}
    failure_codes_by_id = {f.id: f for f in session.scalars(select(FailureCode))}

    def _op_name(operator_id: uuid.UUID | None) -> str | None:
        op = operators_by_id.get(operator_id) if operator_id else None
        return op.display_name if op else None

    def _station_name(station_id: uuid.UUID | None) -> str | None:
        st = stations_by_id.get(station_id) if station_id else None
        return st.name if st else None

    entries: list[dict[str, Any]] = []

    # -- identity/box history: box_assignments + the scans that fall inside
    # each assignment's [assigned_at, released_at-or-now) window (§9.1/§9.2).
    assignments = list(
        session.scalars(
            select(BoxAssignment)
            .where(BoxAssignment.unit_id == unit.id)
            .order_by(BoxAssignment.assigned_at)
        )
    )
    box_ids = [a.box_id for a in assignments]
    boxes_by_id = (
        {b.id: b for b in session.scalars(select(BuildBox).where(BuildBox.id.in_(box_ids)))}
        if box_ids
        else {}
    )
    scan_ids_seen: set[uuid.UUID] = set()
    for a in assignments:
        box = boxes_by_id.get(a.box_id)
        entries.append(
            {
                "at": a.assigned_at,
                "kind": "box_assigned",
                "box_qr": box.qr_payload if box else None,
                "box_label": box.label if box else None,
                "assigned_by": a.assigned_by,
            }
        )
        # ponytail: window-filtered in Python (not SQL) with a small slop
        # margin -- `assigned_at`/`released_at` are app-level `datetime.now()`
        # writes while `scanned_at` is a DB `server_default=now()` write; on
        # sqlite the latter round-trips whole-seconds-only (no microseconds),
        # so a scan a few milliseconds after an assignment in the same
        # wall-clock second can read back as numerically *before* it. A ~2s
        # slop absorbs that truncation (and any real clock skew) without
        # meaningfully widening the window for box-recycling purposes (box
        # reassignments are minutes/hours apart in practice, never <2s).
        slop = timedelta(seconds=2)
        window_start = _naive(a.assigned_at) - slop
        released_or_now = (
            _naive(a.released_at) if a.released_at else _naive(datetime.now(timezone.utc))
        )
        window_end = released_or_now + slop
        scans = session.scalars(select(Scan).where(Scan.box_id == a.box_id)).all()
        for s in scans:
            if s.id in scan_ids_seen:
                continue
            if not (window_start <= _naive(s.scanned_at) <= window_end):
                continue
            scan_ids_seen.add(s.id)
            entries.append(
                {
                    "at": s.scanned_at,
                    "kind": "scan",
                    "result": s.result,
                    "raw_payload": s.raw_payload,
                    "station": _station_name(s.station_id),
                    "operator": _op_name(s.operator_id),
                    "override_by": _op_name(s.override_by),
                }
            )
        if a.released_at is not None:
            entries.append(
                {
                    "at": a.released_at,
                    "kind": "box_released",
                    "box_qr": box.qr_payload if box else None,
                }
            )

    # -- work sessions + pauses (§9.3 time) -----------------------------------
    work_sessions = list(
        session.scalars(
            select(WorkSession)
            .where(WorkSession.unit_id == unit.id)
            .order_by(WorkSession.started_at)
        )
    )
    session_ids = [ws.id for ws in work_sessions]
    pauses = (
        list(
            session.scalars(
                select(SessionPause).where(SessionPause.work_session_id.in_(session_ids))
            )
        )
        if session_ids
        else []
    )
    pauses_by_session: dict[uuid.UUID, list[SessionPause]] = {}
    for p in pauses:
        pauses_by_session.setdefault(p.work_session_id, []).append(p)

    for ws in work_sessions:
        plan_op = plan_op_by_id.get(ws.plan_operation_id)
        entries.append(
            {
                "at": ws.started_at,
                "kind": "session_opened",
                "session_kind": ws.kind,
                "operation": plan_op.title if plan_op else None,
                "operator": _op_name(ws.operator_id),
                "station": _station_name(ws.station_id),
            }
        )
        for p in pauses_by_session.get(ws.id, []):
            entries.append(
                {"at": p.started_at, "kind": "session_paused", "reason_code": p.reason_code}
            )
            if p.ended_at is not None:
                entries.append({"at": p.ended_at, "kind": "session_resumed"})
        if ws.ended_at is not None:
            entries.append(
                {
                    "at": ws.ended_at,
                    "kind": "session_closed",
                    "close_reason": ws.close_reason,
                    "lead_confirmed": ws.lead_confirmed,
                    "operation": plan_op.title if plan_op else None,
                }
            )

    # -- step/substep executions + measurements (§9.3/§9.4) -------------------
    step_execs = list(
        session.scalars(select(StepExecution).where(StepExecution.unit_id == unit.id))
    )
    step_exec_by_id = {se.id: se for se in step_execs}
    step_exec_ids = list(step_exec_by_id)
    sub_execs = (
        list(
            session.scalars(
                select(SubstepExecution).where(SubstepExecution.step_execution_id.in_(step_exec_ids))
            )
        )
        if step_exec_ids
        else []
    )

    for sub in sub_execs:
        step_exec = step_exec_by_id.get(sub.step_execution_id)
        plan_op = plan_op_by_id.get(step_exec.plan_operation_id) if step_exec else None
        at = sub.completed_at or sub.started_at
        if at is None:
            continue  # never started -- nothing to place yet
        step_title, sub_title = (
            _frozen_titles(plan_op, step_exec.step_seq, sub.substep_seq)
            if step_exec else (None, None)
        )
        entries.append(
            {
                "at": at,
                "kind": f"substep_{sub.status}",
                "operation": plan_op.title if plan_op else None,
                "step_title": step_title,
                "substep_title": sub_title,
                "type": sub.type,
                "value_numeric": _num(sub.value_numeric),
                "value_text": sub.value_text,
                "pass": sub.pass_,
                "out_of_tolerance": sub.out_of_tolerance,
                "disposition": sub.disposition,
                "skip_reason": sub.skip_reason,
                "skip_authorized_by": _op_name(sub.skip_authorized_by),
                "notes": sub.notes,
                "superseded": sub.superseded,
                "operator": _op_name(sub.operator_id),
            }
        )

    sub_exec_ids = [s.id for s in sub_execs]
    measurements = (
        list(
            session.scalars(
                select(Measurement).where(Measurement.substep_execution_id.in_(sub_exec_ids))
            )
        )
        if sub_exec_ids
        else []
    )
    for m in measurements:
        entries.append(
            {
                "at": m.recorded_at,
                "kind": "measurement",
                "name": m.name,
                "value": _num(m.value),
                "unit": m.unit,
                "nominal": _num(m.nominal),
                "tol_plus": _num(m.tol_plus),
                "tol_minus": _num(m.tol_minus),
                "in_tolerance": m.in_tolerance,
                "gauge_id": m.gauge_id,
                "operator": _op_name(m.recorded_by),
            }
        )

    # -- failures / scrap (§9.4/§9.6) ------------------------------------------
    failures = list(
        session.scalars(
            select(Failure).where(Failure.unit_id == unit.id).order_by(Failure.detected_at)
        )
    )
    scraps_by_failure = {
        s.failure_id: s
        for s in session.scalars(select(ScrapEvent).where(ScrapEvent.unit_id == unit.id))
    }
    for f in failures:
        fc = failure_codes_by_id.get(f.failure_code_id)
        rework_op = plan_op_by_id.get(f.rework_to_op) if f.rework_to_op else None
        entries.append(
            {
                "at": f.detected_at,
                "kind": "failure",
                "failure_code": fc.code if fc else None,
                "failure_label": fc.label if fc else None,
                "narrative": f.narrative,
                "disposition": f.disposition,
                "rework_to_op": rework_op.title if rework_op else None,
                "detected_by": _op_name(f.detected_by),
                "authorized_by": _op_name(f.authorized_by),
            }
        )
        scrap = scraps_by_failure.get(f.id)
        if scrap is not None:
            replacement = (
                session.get(Unit, scrap.replacement_unit_id) if scrap.replacement_unit_id else None
            )
            entries.append(
                {
                    "at": scrap.created_at,
                    "kind": "scrap",
                    "cause_code": scrap.cause_code,
                    "narrative": scrap.narrative,
                    "material_value_est": _num(scrap.material_value_est),
                    "authorized_by": _op_name(scrap.authorized_by),
                    "replacement_unit_no": replacement.unit_no if replacement else None,
                }
            )

    # -- transits (§9.2) --------------------------------------------------------
    transits = list(
        session.scalars(
            select(Transit).where(Transit.unit_id == unit.id).order_by(Transit.departed_at)
        )
    )
    for t in transits:
        entries.append(
            {
                "at": t.departed_at,
                "kind": "transit_departed",
                "from_station": _station_name(t.from_station_id),
                "to_station": _station_name(t.to_station_id),
            }
        )
        if t.arrived_at is not None:
            entries.append(
                {
                    "at": t.arrived_at,
                    "kind": "transit_arrived",
                    "to_station": _station_name(t.to_station_id),
                    "seconds": t.seconds,
                }
            )

    # -- unit-level audit events not otherwise narrated by a detail row -------
    for ev in events_domain.timeline(session, unit_id=unit.id):
        entries.append(
            {
                "at": ev.at,
                "kind": f"event_{ev.verb}",
                "verb": ev.verb,
                "actor": _op_name(ev.actor_id),
                "before": ev.before,
                "after": ev.after,
            }
        )

    for e in entries:
        e["category"] = _category(e["kind"])
    entries.sort(key=lambda e: _naive(e["at"]))

    return {
        "unit": {
            "id": unit.id,
            "unit_no": unit.unit_no,
            "serial_number": unit.serial_number,
            "status": unit.status,
            "first_pass": unit.first_pass,
            "rework_count": unit.rework_count,
            "remake_of_unit_id": unit.remake_of_unit_id,
            "completed_at": unit.completed_at,
        },
        "work_order": {
            "id": work_order.id if work_order else None,
            "qty": work_order.qty if work_order else None,
            "status": work_order.status if work_order else None,
            "plan_version": work_order.plan_version if work_order else None,
            "due_date": work_order.due_date if work_order else None,
        },
        "product": {"id": product.id, "name": product.name} if product else None,
        "jb2": {
            "job_number": (line_item.payload or {}).get("jobNumber") if line_item else None,
            "order_number": (line_item.payload or {}).get("orderNumber") if line_item else None,
            "part_number": line_item.part_number if line_item else None,
        },
        "plan_operations": [
            {
                "seq": op.seq,
                "title": op.title,
                "operation_code": op.operation_code,
                "instruction_set_id": (op.frozen_content or {}).get("instruction_set_id"),
                "instruction_version": (op.frozen_content or {}).get("version"),
                "status": op.status,
                "est_minutes": op.est_minutes,
            }
            for op in plan_ops
        ],
        "timeline": entries,
    }


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (uuid.UUID, datetime, date)):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


@router.get("/units/{unit_id}")
def unit_detail(request: Request, unit_id: uuid.UUID, session: Session = Depends(get_session)):
    payload = build_unit_timeline(session, unit_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="unit not found")
    return templates.TemplateResponse(request, "unit/detail.html", payload)


@router.get("/api/v1/units/{unit_id}/timeline")
def unit_timeline_json(unit_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    payload = build_unit_timeline(session, unit_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="unit not found")
    return _to_jsonable(payload)
