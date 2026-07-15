"""Station kiosk UI (P3-05/P3-06/P3-07) -- DD §12, CR-009 (Android tablets,
manual-entry fallback beside every scan input).

Server-rendered HTMX + Alpine shell (CR-003). The tablet holds no state
machine (§12.1 "thin client"): every screen here is a GET that reads
current DB state (scans/sessions/substeps all mutate through
POST /scan (app/api/scan.py, not this file) and
POST /station/... (app/api/substeps.py)). WebSocket push (queue/lead
updates) is P4-01 -- the idle screen polls `GET /station/queue` on a plain
10 s htmx interval instead.

Kiosk enrollment: hardware/kiosk provisioning is out of scope (CR-009), but
the *browser* still needs a way to carry the station's kiosk token as a
cookie so every request here resolves via `deps.require_station` --
`/station/enroll` is that one-time step (visit once on the tablet, cookie
persists across reboots since the kiosk browser stays pointed at
`/station`).
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import deps, service
from app.config import REPO_ROOT, config
from app.db import get_session
from app.domain import failures, library, statemachine, substeps
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import Operator, Station
from app.domain.models_jb2 import JB2OrderLineItem, JB2OrderRouting
from app.domain.models_library import Product

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

STATION_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 10  # ~10y -- kiosk survives reboots (§12.1)


def _resolve_station_soft(request: Request, session: Session) -> Station | None:
    """Like `deps.require_station` but returns None instead of raising --
    used only by the idle screen, which wants to redirect to enrollment
    rather than 401 on a bare tablet. NOT `deps.require_station` called
    directly: that dependency's `x_station_token` parameter carries a
    FastAPI `Header(...)` marker as its Python-level default, so invoking it
    outside FastAPI's own DI (i.e. as a plain function call) would bind that
    marker object itself, not None -- reading the header here explicitly
    sidesteps that footgun."""
    token = request.headers.get("X-Station-Token") or request.cookies.get(deps.STATION_COOKIE)
    if not token:
        return None
    return service.resolve_station_by_token(session, token)


def _optional_operator(
    request: Request, response: Response, station: Station, session: Session
) -> Operator | None:
    """Same swallow-401-into-None pattern as app/api/scan.py's
    `_optional_operator` -- the idle screen renders fine with nobody badged
    in yet (S3's "operator_required" is a scan-time rejection, not a
    page-render error)."""
    try:
        return deps.require_operator(request, response, station, session)
    except HTTPException:
        return None


# -- kiosk enrollment (manual token entry, CR-009) -------------------------------


@router.get("/station/enroll")
def station_enroll_form(request: Request):
    return templates.TemplateResponse(
        request, "station/enroll.html",
        {
            "error": request.query_params.get("error"),
            "prefill_token": request.query_params.get("token", ""),
        },
    )


@router.post("/station/enroll")
def station_enroll_submit(
    response: Response, token: str = Form(...), session: Session = Depends(get_session)
):
    station = service.resolve_station_by_token(session, token.strip())
    if station is None:
        return RedirectResponse(
            "/station/enroll?error=invalid+or+inactive+station+token", status_code=303
        )
    redirect = RedirectResponse("/station", status_code=303)
    redirect.set_cookie(
        deps.STATION_COOKIE, token.strip(), httponly=True, samesite="lax",
        max_age=STATION_COOKIE_MAX_AGE,
    )
    return redirect


# -- idle screen + queue (P3-05) -------------------------------------------------


def _op_work_center(session: Session, plan_op: PlanOperation | None) -> str | None:
    if plan_op is None or plan_op.jb2_routing_id is None:
        return None
    routing = session.get(JB2OrderRouting, plan_op.jb2_routing_id)
    return routing.work_center_code if routing else None


def _queue_rows(session: Session, station: Station) -> list[dict]:
    """Units inbound to or at this station: next-op work center matches,
    status in (queued, at_station, in_transit) -- brief's literal
    derivation, no separate "queue" table.

    ponytail: one query per candidate unit (unit_next_op + a routing
    lookup) rather than a single joined query -- `unit_next_op` already
    lazily persists `units.current_plan_op_id`, so repeat polls settle into
    O(1) per unit. Fine at floor scale (tens of concurrent units per
    station); revisit with a materialized queue view only if this shows up
    hot in practice (same ceiling-noted pattern as ESC-002)."""
    if station.work_center_code is None:
        return []
    candidates = session.scalars(
        select(Unit).where(Unit.status.in_(("queued", "at_station", "in_transit")))
    ).all()
    rows = []
    for unit in candidates:
        plan_op = statemachine.unit_next_op(session, unit)
        if plan_op is None or _op_work_center(session, plan_op) != station.work_center_code:
            continue
        work_order = session.get(WorkOrder, unit.work_order_id)
        product = session.get(Product, work_order.product_id) if work_order else None
        rows.append(
            {"unit": unit, "work_order": work_order, "product": product, "plan_op": plan_op}
        )
    rows.sort(key=lambda r: (r["work_order"].due_date or date.max, r["unit"].unit_no))
    return rows


@router.get("/station")
def station_idle(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    station = _resolve_station_soft(request, session)
    if station is None:
        return RedirectResponse("/station/enroll", status_code=303)

    operator = _optional_operator(request, response, station, session)
    queue = _queue_rows(session, station)
    return templates.TemplateResponse(
        request, "station/idle.html",
        {
            "station": station, "operator": operator, "queue": queue,
            "error": request.query_params.get("error"),
        },
    )


@router.get("/station/queue")
def station_queue_partial(
    request: Request,
    station: Station = Depends(deps.require_station),
    session: Session = Depends(get_session),
):
    queue = _queue_rows(session, station)
    return templates.TemplateResponse(
        request, "station/_queue.html", {"station": station, "queue": queue}
    )


@router.get("/station/offline")
def station_offline(request: Request):
    return templates.TemplateResponse(request, "station/offline.html", {})


# -- scan-result + wrong-station screens (P3-05) ---------------------------------


@router.get("/station/scan-result/{unit_id}")
def station_scan_result(
    unit_id: uuid.UUID,
    request: Request,
    response: Response,
    station: Station = Depends(deps.require_station),
    session: Session = Depends(get_session),
):
    operator = _optional_operator(request, response, station, session)
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unit not found")

    work_order = session.get(WorkOrder, unit.work_order_id)
    product = session.get(Product, work_order.product_id) if work_order else None
    line_item = (
        session.get(JB2OrderLineItem, work_order.jb2_line_item_id)
        if work_order is not None else None
    )
    plan_op = statemachine.unit_next_op(session, unit)
    steps = plan_op.frozen_content.get("steps", []) if plan_op and plan_op.frozen_content else []
    status_map = substeps.step_status_map(session, plan_op, unit) if plan_op else {}
    done_steps = sum(1 for s in steps if status_map.get(s["seq"]) == "done")

    work_session = None
    if plan_op is not None and operator is not None:
        work_session = substeps.open_work_session(
            session, unit=unit, plan_op=plan_op, operator=operator, station=station
        )

    return templates.TemplateResponse(
        request, "station/scan_result.html",
        {
            "station": station, "operator": operator, "unit": unit, "work_order": work_order,
            "product": product, "line_item": line_item, "plan_op": plan_op,
            "total_steps": len(steps), "done_steps": done_steps, "work_session": work_session,
            "error": request.query_params.get("error"),
        },
    )


@router.get("/station/wrong-station")
def station_wrong_station(
    request: Request,
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
):
    return templates.TemplateResponse(
        request, "station/wrong_station.html",
        {
            "station": station, "operator": operator,
            "payload": request.query_params.get("payload", ""),
            "expected_work_center": request.query_params.get("expected_work_center", ""),
            "operation_title": request.query_params.get("operation_title", ""),
            "error": request.query_params.get("error"),
        },
    )


# -- execution screen (P3-06/P3-07) ----------------------------------------------


@router.get("/station/execute/{unit_id}")
def station_execute(
    unit_id: uuid.UUID,
    request: Request,
    step: int | None = None,
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unit not found")

    plan_op = statemachine.unit_next_op(session, unit)
    if plan_op is None:
        return RedirectResponse(
            f"/station/scan-result/{unit_id}?error=no+active+operation+for+this+unit",
            status_code=303,
        )

    work_session = substeps.open_work_session(
        session, unit=unit, plan_op=plan_op, operator=operator, station=station
    )
    if work_session is None:
        return RedirectResponse(
            f"/station/scan-result/{unit_id}?error=scan+the+box+to+start+a+session", status_code=303
        )

    steps = sorted(plan_op.frozen_content.get("steps", []), key=lambda s: s["seq"])
    step_status = substeps.step_status_map(session, plan_op, unit)
    exec_map = substeps.substep_execution_map(session, plan_op, unit)
    remaining = substeps.remaining_required_count(session, plan_op, unit)

    # Failure-code picker + rework-to-op picker for the measurement
    # disposition dialog (P3-R2/ESC-004): product-scoped + global codes for
    # this unit's product, and every plan op at/before the current one (a
    # rework_to_op target may never be later than where the failure was
    # caught, app/domain/failures.py's own check).
    work_order = session.get(WorkOrder, unit.work_order_id)
    failure_codes = library.list_failure_codes(
        session, work_order.product_id if work_order else None
    )
    rework_ops = session.scalars(
        select(PlanOperation)
        .where(
            PlanOperation.work_order_id == unit.work_order_id,
            PlanOperation.seq <= plan_op.seq,
        )
        .order_by(PlanOperation.seq)
    ).all()

    current_seq = step if step is not None else substeps.current_step_seq(session, plan_op, unit)
    current_step = next((s for s in steps if s["seq"] == current_seq), steps[0] if steps else None)

    # shim for templates/partials/instruction_steps.html's `iset` context --
    # frozen_content only carries instruction_set_id/version, no title/state
    # (that lives on the live InstructionSet row, which this frozen plan
    # deliberately never references again -- see models_execution.py).
    iset_shim = {
        "title": plan_op.title, "version": plan_op.frozen_content.get("version"),
        "state": "frozen", "est_minutes": plan_op.est_minutes,
    }
    tree = (
        [{"step": current_step, "substeps": current_step.get("substeps", [])}]
        if current_step else []
    )

    return templates.TemplateResponse(
        request, "station/execute.html",
        {
            "station": station, "operator": operator, "unit": unit, "plan_op": plan_op,
            "steps": steps, "step_status": step_status, "exec_map": exec_map,
            "current_step": current_step, "iset": iset_shim, "tree": tree,
            "work_session": work_session, "remaining": remaining,
            "failure_codes": failure_codes, "rework_ops": rework_ops,
            "error": request.query_params.get("error"),
        },
    )


# -- whole-operation Fail screen (P3-R2 remediation, ESC-004) --------------------
#
# The deep failure/rework/scrap flow (app/domain/failures.record_failure,
# DD §6.5) wired to its own kiosk screen: failure-code picker + narrative +
# a 4-way disposition choice, same taxonomy/target-op pickers as the
# measurement dialog above. `rework_to_op`/`scrap` require a lead second
# badge (O4/O5); `use_as_is` accepts lead-or-quality (O3, same role set as
# app/domain/substeps.py's DISPOSITION_ROLES -- reused rather than
# redeclared). `rework_in_place` needs none.


@router.get("/station/fail/{unit_id}")
def station_fail_form(
    unit_id: uuid.UUID,
    request: Request,
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unit not found")
    plan_op = statemachine.unit_next_op(session, unit)
    if plan_op is None:
        return RedirectResponse(
            f"/station/scan-result/{unit_id}?error=no+active+operation+for+this+unit",
            status_code=303,
        )

    work_order = session.get(WorkOrder, unit.work_order_id)
    product = session.get(Product, work_order.product_id) if work_order else None
    failure_codes = library.list_failure_codes(
        session, work_order.product_id if work_order else None
    )
    rework_ops = session.scalars(
        select(PlanOperation)
        .where(
            PlanOperation.work_order_id == unit.work_order_id,
            PlanOperation.seq <= plan_op.seq,
        )
        .order_by(PlanOperation.seq)
    ).all()

    return templates.TemplateResponse(
        request, "station/fail.html",
        {
            "station": station, "operator": operator, "unit": unit, "work_order": work_order,
            "product": product, "plan_op": plan_op, "failure_codes": failure_codes,
            "rework_ops": rework_ops, "error": request.query_params.get("error"),
        },
    )


@router.post("/station/fail/{unit_id}")
def station_fail_submit(
    unit_id: uuid.UUID,
    disposition: str = Form(...),
    failure_code_id: uuid.UUID = Form(...),
    narrative: str = Form(""),
    rework_to_op_seq: int | None = Form(None),
    override_badge: str = Form(""),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    unit = session.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unit not found")
    plan_op = statemachine.unit_next_op(session, unit)
    if plan_op is None:
        return RedirectResponse(
            f"/station/scan-result/{unit_id}?error=no+active+operation+for+this+unit",
            status_code=303,
        )

    roles = substeps.DISPOSITION_ROLES.get(disposition)
    try:
        authorizer = None
        if roles:
            if not override_badge:
                raise service.SecondBadgeError(
                    f"disposition '{disposition}' requires a badge scan from one of {roles}"
                )
            authorizer = deps.second_badge_any(
                session, payload=override_badge, roles=roles, actor=operator
            )

        rework_to_op = None
        if disposition == "rework_to_op":
            if rework_to_op_seq is None:
                raise ValueError("rework_to_op disposition requires a target operation")
            rework_to_op = session.scalars(
                select(PlanOperation).where(
                    PlanOperation.work_order_id == unit.work_order_id,
                    PlanOperation.seq == rework_to_op_seq,
                )
            ).first()
            if rework_to_op is None:
                raise ValueError(f"no operation at seq {rework_to_op_seq} on this work order")

        failures.record_failure(
            session, unit, plan_op, None, failure_code_id, narrative or None, operator,
            disposition, rework_to_op=rework_to_op, authorized_by=authorizer,
        )
        session.commit()
    except (ValueError, service.SecondBadgeError) as exc:
        session.rollback()
        return RedirectResponse(
            f"/station/fail/{unit_id}?error={quote(str(exc))}", status_code=303
        )

    # scrap/rework_to_op: the unit leaves this operation (scrapped, or
    # in_transit to op K) -- back to idle. use_as_is/rework_in_place: the
    # unit stays put, operator resumes the same operation.
    if disposition in ("scrap", "rework_to_op"):
        return RedirectResponse("/station", status_code=303)
    return RedirectResponse(f"/station/execute/{unit_id}", status_code=303)


# -- photo attachment retrieval (P3-12) -------------------------------------------


@router.get("/artifacts/execution/{unit_id}/{name}")
def get_execution_attachment(unit_id: uuid.UUID, name: str) -> FileResponse:
    base = (Path(config.artifact_dir or "./artifacts") / "execution" / str(unit_id)).resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)
