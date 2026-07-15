"""Backfill screen (P4-06) -- DD §17.2: "on recovery, a lead uses the
backfill screen to enter paper data (entries flagged `backfilled=true`,
original paper retained)."

BACKFILL-MARKER DECISION (documented per the task brief, since this choice
governs the whole module): `SubstepExecution`/`StepExecution`/`WorkSession`
have no `backfilled` column (checked -- app/domain/models_floor.py) and this
task adds no migration for one. Every substep/measurement/material/signoff/
finish call this module makes is a **live, unmodified call** into
`app.domain.substeps` / `app.domain.statemachine` / `app.outbox.payloads` --
the exact same functions the station kiosk calls, so the effects (step/
substep rows, events, WorkSession, jb2_outbox writes) are byte-for-byte
identical to a live entry. Immediately after each such call, this module
emits one extra `backfill.recorded` event (new verb, app/domain/events.py)
on the SAME entity the live call just touched (`entity_id` matches), so any
per-entity timeline or metrics query built from those tables picks the
extra event up for free -- that's what makes a backfilled row
*distinguishable* without touching a single shared model or migration. The
substep's own `notes` field also gets a "[BACKFILLED]" prefix so it reads
plainly even in a view that never looks at the event stream.

Everything is entered under one performed-by operator + one paper
timestamp (DD's brief: "who did it and when" -- singular, not a
per-substep start/stop) and one WorkSession opened+closed at that same
timestamp. ponytail: this means the labor duration attributed to a
backfilled session is always zero (`started_at == ended_at`) -- the paper
traveler this replaces has no separate start/stop time to give it either.
A future refinement (separate start/finish paper timestamps) is a small,
additive form-field change if accurate backfilled labor-hours ever matters;
not built now (YAGNI -- nobody asked for backfilled time-ticket precision,
only for the data and the JB2 write to exist).

A dedicated "Paper Backfill (admin)" station is lazily created (via the
existing `app.auth.service.create_station`, unmodified) so this module
never has to touch `app/auth/deps.py`'s kiosk-cookie auth chain -- this is
an office/admin screen (same class as `/admin/health`, `/admin/library`),
not a kiosk route, so it needs no station token / operator cookie. It only
needs a `Station` row to satisfy the domain functions' required parameter.

Auth model: a lead operator (role `lead`) must be selected to authorize the
entry (validated server-side regardless of what the <select> offers,
matching this codebase's existing belt-and-suspenders pattern e.g.
`app/api/substeps.py`'s disposition role checks). No badge scanner on an
office desktop -- picking from a list is the manual-entry fallback (CR-009
already established this pattern for hardware-bypassed flows).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import service
from app.config import REPO_ROOT, config
from app.db import get_session
from app.domain import events, statemachine, substeps
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    Operator,
    Station,
    StepExecution,
    SubstepExecution,
    Transit,
)
from app.domain.models_library import FailureCode, Product
from app.outbox import payloads

router = APIRouter(prefix="/admin/backfill")
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

BACKFILL_STATION_NAME = "Paper Backfill (admin)"


class BackfillError(Exception):
    """Rejected backfill submission -- mapped to a redirect-with-error, same
    UX convention as app/api/library.py / app/api/substeps.py."""


# -- helpers ------------------------------------------------------------------


def _get_backfill_station(session: Session) -> Station:
    station = session.execute(
        select(Station).where(Station.name == BACKFILL_STATION_NAME)
    ).scalars().first()
    if station is None:
        station, _token = service.create_station(session, name=BACKFILL_STATION_NAME)
    return station


def _require_lead(session: Session, operator_id: uuid.UUID | None) -> Operator:
    if operator_id is None:
        raise BackfillError("an authorizing lead is required")
    lead = session.get(Operator, operator_id)
    if lead is None or not lead.active:
        raise BackfillError("selected lead is not a known active operator")
    if "lead" not in (lead.roles or []):
        raise BackfillError(f"'{lead.display_name}' does not hold the lead role")
    return lead


def _require_operator(session: Session, operator_id: uuid.UUID | None, *, field: str) -> Operator:
    if operator_id is None:
        raise BackfillError(f"{field} is required")
    operator = session.get(Operator, operator_id)
    if operator is None or not operator.active:
        raise BackfillError(f"{field}: not a known active operator")
    return operator


def _parse_paper_at(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BackfillError("'when' timestamp is not a valid date/time") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _sub_exec(
    session: Session, plan_op: PlanOperation, unit: Unit, step_seq: int, substep_seq: int
) -> SubstepExecution:
    return session.execute(
        select(SubstepExecution)
        .join(StepExecution, SubstepExecution.step_execution_id == StepExecution.id)
        .where(
            StepExecution.plan_operation_id == plan_op.id,
            StepExecution.unit_id == unit.id,
            StepExecution.step_seq == step_seq,
            SubstepExecution.substep_seq == substep_seq,
            SubstepExecution.superseded.is_(False),
        )
    ).scalars().one()


def _mark_backfilled(
    session: Session,
    entity: Any,
    *,
    lead: Operator,
    performed_by: Operator,
    station: Station,
    paper_at: datetime,
    note: str | None = None,
) -> None:
    """The one extra event that makes a live-path row distinguishable as
    backfilled -- see module docstring's BACKFILL-MARKER DECISION."""
    after = {
        "backfilled": True,
        "performed_by": str(performed_by.id),
        "lead_id": str(lead.id),
        "paper_at": paper_at.isoformat(),
    }
    if note:
        after["note"] = note
    events.emit(
        session, "backfill.recorded", entity=entity, actor_id=lead.id, station_id=station.id,
        after=after,
    )


def _tag_notes(notes: str) -> str:
    return f"[BACKFILLED] {notes}" if notes else "[BACKFILLED]"


def _eligible_units(session: Session) -> list[dict]:
    rows = session.execute(
        select(Unit, WorkOrder, Product)
        .join(WorkOrder, WorkOrder.id == Unit.work_order_id)
        .join(Product, Product.id == WorkOrder.product_id)
        .where(Unit.status.notin_(("done", "scrapped")))
        .order_by(Unit.unit_no)
    ).all()
    out = []
    for unit, work_order, product in rows:
        plan_op = statemachine.unit_next_op(session, unit)
        out.append(
            {
                "unit": unit, "work_order": work_order, "product": product, "plan_op": plan_op,
            }
        )
    session.commit()  # persists any lazily-resolved current_plan_op_id pointers
    return out


# -- GET: pick a unit -----------------------------------------------------------


@router.get("")
def backfill_picker(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "admin/backfill.html",
        {
            "mode": "picker",
            "units": _eligible_units(session),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/{unit_id}")
def backfill_form(request: Request, unit_id: uuid.UUID, session: Session = Depends(get_session)):
    unit = session.get(Unit, unit_id)
    if unit is None:
        return RedirectResponse("/admin/backfill?error=" + quote("unit not found"), status_code=303)
    plan_op = statemachine.unit_next_op(session, unit)
    session.commit()
    if plan_op is None:
        return RedirectResponse(
            "/admin/backfill?error=" + quote("unit has no active operation"), status_code=303
        )

    operators = session.execute(
        select(Operator).where(Operator.active.is_(True)).order_by(Operator.display_name)
    ).scalars().all()
    leads = [op for op in operators if "lead" in (op.roles or [])]
    failure_codes = session.execute(
        select(FailureCode).where(FailureCode.active.is_(True)).order_by(FailureCode.code)
    ).scalars().all()
    remaining = substeps.remaining_required_count(session, plan_op, unit)
    exec_map = substeps.substep_execution_map(session, plan_op, unit)

    return templates.TemplateResponse(
        request, "admin/backfill.html",
        {
            "mode": "form",
            "unit": unit,
            "plan_op": plan_op,
            "operators": operators,
            "leads": leads,
            "failure_codes": failure_codes,
            "remaining": remaining,
            "exec_map": exec_map,
            "error": request.query_params.get("error"),
        },
    )


# -- POST: submit ---------------------------------------------------------------


@router.post("/{unit_id}")
async def backfill_submit(
    request: Request, unit_id: uuid.UUID, session: Session = Depends(get_session)
):
    form = await request.form()

    def _uuid_field(name: str) -> uuid.UUID | None:
        raw = form.get(name)
        return uuid.UUID(raw) if raw else None

    try:
        unit = session.get(Unit, unit_id)
        if unit is None:
            raise BackfillError("unit not found")
        plan_op = statemachine.unit_next_op(session, unit)
        if plan_op is None:
            raise BackfillError("unit has no active operation")

        lead = _require_lead(session, _uuid_field("lead_operator_id"))
        performed_by = _require_operator(
            session, _uuid_field("performed_by_operator_id"), field="performed by"
        )
        paper_at = _parse_paper_at(form.get("paper_at"))
        station = _get_backfill_station(session)
        finish_requested = form.get("finish") == "on"

        work_session = statemachine.open_session(
            session, unit=unit, operator=performed_by, station=station, plan_op=plan_op,
            kind="first_pass", now=paper_at,
        )

        for step in plan_op.frozen_content.get("steps", []):
            step_seq = step["seq"]
            for sub in step.get("substeps", []):
                substep_seq = sub["seq"]
                prefix = f"sub_{step_seq}_{substep_seq}_"
                action = form.get(prefix + "action", "leave")
                if action == "leave":
                    continue
                notes = _tag_notes(form.get(prefix + "notes", ""))

                if action == "done":
                    substeps.complete_substep(
                        session, station=station, operator=performed_by, unit_id=unit.id,
                        step_seq=step_seq, substep_seq=substep_seq, notes=notes, now=paper_at,
                    )
                    _mark_backfilled(
                        session, _sub_exec(session, plan_op, unit, step_seq, substep_seq),
                        lead=lead, performed_by=performed_by, station=station, paper_at=paper_at,
                    )
                elif action == "fail":
                    substeps.fail_substep(
                        session, station=station, operator=performed_by, unit_id=unit.id,
                        step_seq=step_seq, substep_seq=substep_seq, notes=notes, now=paper_at,
                    )
                    _mark_backfilled(
                        session, _sub_exec(session, plan_op, unit, step_seq, substep_seq),
                        lead=lead, performed_by=performed_by, station=station, paper_at=paper_at,
                    )
                elif action == "skip":
                    substeps.skip_substep(
                        session, station=station, operator=performed_by, unit_id=unit.id,
                        step_seq=step_seq, substep_seq=substep_seq,
                        skip_reason=(
                            form.get(prefix + "skip_reason") or "paper backfill: not performed"
                        ),
                        lead=lead, now=paper_at,
                    )
                    _mark_backfilled(
                        session, _sub_exec(session, plan_op, unit, step_seq, substep_seq),
                        lead=lead, performed_by=performed_by, station=station, paper_at=paper_at,
                    )
                elif action == "measurement":
                    value = form.get(prefix + "value")
                    if not value:
                        raise BackfillError(
                            f"step {step_seq} substep {substep_seq}: value required"
                        )
                    result = substeps.record_measurement(
                        session, station=station, operator=performed_by, unit_id=unit.id,
                        step_seq=step_seq, substep_seq=substep_seq, value=value,
                        gauge_id=form.get(prefix + "gauge_id") or None, notes=notes, now=paper_at,
                    )
                    sub_exec = _sub_exec(session, plan_op, unit, step_seq, substep_seq)
                    _mark_backfilled(
                        session, sub_exec, lead=lead, performed_by=performed_by, station=station,
                        paper_at=paper_at,
                    )
                    disposition = form.get(prefix + "disposition")
                    if result["status"] == "failed" and disposition:
                        rework_seq = form.get(prefix + "rework_to_op_seq")
                        # authorizer only when the live disposition dialog
                        # would have required a second badge (DISPOSITION_
                        # ROLES) -- rework_in_place needs none, matching
                        # app/api/substeps.py's own role lookup exactly.
                        needs_authorizer = bool(substeps.DISPOSITION_ROLES.get(disposition))
                        substeps.apply_disposition(
                            session, station=station, operator=performed_by, unit_id=unit.id,
                            step_seq=step_seq, substep_seq=substep_seq, disposition=disposition,
                            failure_code_id=uuid.UUID(form[prefix + "failure_code_id"]),
                            notes=notes, authorizer=lead if needs_authorizer else None,
                            rework_to_op_seq=int(rework_seq) if rework_seq else None, now=paper_at,
                        )
                        _mark_backfilled(
                            session, sub_exec, lead=lead, performed_by=performed_by,
                            station=station, paper_at=paper_at, note=f"disposition:{disposition}",
                        )
                elif action == "material":
                    substitution_for = form.get(prefix + "substitution_for") or None
                    substeps.record_material(
                        session, station=station, operator=performed_by, unit_id=unit.id,
                        step_seq=step_seq, substep_seq=substep_seq,
                        part_number=form.get(prefix + "part_number", ""),
                        qty_used=form.get(prefix + "qty_used", "0"),
                        qty_scrapped=form.get(prefix + "qty_scrapped", "0"),
                        lot=form.get(prefix + "lot") or None, uom=form.get(prefix + "uom") or None,
                        substitution_for=substitution_for,
                        # authorizer only when live would have required one
                        # (a lead-authorized substitution) -- matching the
                        # live path exactly rather than always stamping the
                        # backfill lead as the material authorizer.
                        authorizer=lead if substitution_for else None, now=paper_at,
                    )
                    _mark_backfilled(
                        session, _sub_exec(session, plan_op, unit, step_seq, substep_seq),
                        lead=lead, performed_by=performed_by, station=station, paper_at=paper_at,
                    )
                elif action == "signoff":
                    substeps.complete_signoff(
                        session, station=station, operator=performed_by, unit_id=unit.id,
                        step_seq=step_seq, substep_seq=substep_seq, authorizer=lead, now=paper_at,
                    )
                    _mark_backfilled(
                        session, _sub_exec(session, plan_op, unit, step_seq, substep_seq),
                        lead=lead, performed_by=performed_by, station=station, paper_at=paper_at,
                    )

        outbox_id = None
        if finish_requested:
            if substeps.remaining_required_count(session, plan_op, unit) > 0:
                raise BackfillError("cannot finish: required substeps still incomplete")
            next_op_peek = session.scalars(
                select(PlanOperation).where(
                    PlanOperation.work_order_id == unit.work_order_id,
                    PlanOperation.seq > plan_op.seq, PlanOperation.status != "skipped",
                ).order_by(PlanOperation.seq)
            ).first()
            if (
                next_op_peek is None and config.require_serial_before_done
                and not unit.serial_number
            ):
                raise BackfillError("serial number required before this unit can be marked done")

            statemachine.close_session(
                session, work_session, reason="finished", actor_id=performed_by.id, now=paper_at,
            )
            outbox_id = payloads.enqueue_finish_writeback(
                session, work_session, unit, plan_op, pieces_finished=1, pieces_scrapped=0,
            )
            _mark_backfilled(
                session, work_session, lead=lead, performed_by=performed_by, station=station,
                paper_at=paper_at, note="finish",
            )

            now_real = datetime.now(timezone.utc)
            next_op = statemachine.unit_op_done(session, unit, plan_op)
            if next_op is not None:
                unit.status = "in_transit"
                session.add(
                    Transit(unit_id=unit.id, from_station_id=station.id, departed_at=now_real)
                )
                events.emit(
                    session, "unit.moved", entity=unit, actor_id=performed_by.id,
                    station_id=station.id,
                    after={
                        "from_plan_operation_id": str(plan_op.id),
                        "to_plan_operation_id": str(next_op.id), "backfilled": True,
                    },
                )
            else:
                unit.status = "done"
                unit.completed_at = now_real
                events.emit(
                    session, "unit.done", entity=unit, actor_id=performed_by.id,
                    station_id=station.id, after={"backfilled": True},
                )
        else:
            # No finish requested this submission -- close the session as
            # clocked_out so it doesn't dangle open forever (mirrors the
            # kiosk footer's own Clock-out control); a later backfill
            # submission (or a live operator) reopens a fresh session.
            statemachine.close_session(
                session, work_session, reason="clocked_out", actor_id=performed_by.id, now=paper_at,
            )

        session.commit()
    except (BackfillError, substeps.SubstepError, ValueError) as exc:
        session.rollback()
        return RedirectResponse(
            f"/admin/backfill/{unit_id}?error={quote(str(exc))}", status_code=303
        )

    dest = "/admin/backfill" if finish_requested else f"/admin/backfill/{unit_id}"
    suffix = "&outbox_id=" + str(outbox_id) if outbox_id else ""
    return RedirectResponse(dest + ("?ok=1" + suffix), status_code=303)
