"""Substep execution API (P3-06/P3-07/P3-12) -- DD §6.4/§9.5/§12.3.

Every route here: requires station + operator (both floor auth
dependencies, DD §14), resolves/validates entirely through
`app.domain.substeps` (the single validator, same split as
app/api/scan.py -> app/domain/statemachine.py), and responds with a 303
redirect back to the execution screen -- plain-form kiosk UX (matches
app/api/library.py / app/api/media.py's redirect-with-error convention),
not scan.py's JSON-API style, since these are tappable forms, not fetch
calls from station.js.

Idempotency: every form carries a client `request_id` (crypto.randomUUID(),
set by static/station.js); wrapped through
`app.domain.statemachine.with_request_dedup` per state-machine.md §7.
"""
from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.media import ALLOWED_CONTENT_TYPES, MAX_UPLOAD_BYTES
from app.auth import deps, service
from app.db import get_session
from app.domain import statemachine, substeps
from app.domain.models_floor import Operator, Station, WorkSession

router = APIRouter(prefix="/station")


def _back(unit_id: uuid.UUID, step_seq: int, error: str | None = None) -> RedirectResponse:
    url = f"/station/execute/{unit_id}?step={step_seq}"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url, status_code=303)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/start")
def station_substep_start(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    try:
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/start",
            lambda: substeps.start_substep(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq,
            ),
        )
        session.commit()
    except substeps.SubstepError as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/complete")
def station_substep_complete(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    notes: str = Form(""),
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    try:
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/complete",
            lambda: substeps.complete_substep(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq, notes=notes or None,
            ),
        )
        session.commit()
    except substeps.SubstepError as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/fail")
def station_substep_fail(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    notes: str = Form(""),
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    try:
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/fail",
            lambda: substeps.fail_substep(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq, notes=notes or None,
            ),
        )
        session.commit()
    except substeps.SubstepError as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/skip")
def station_substep_skip(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    skip_reason: str = Form(...),
    override_badge: str = Form(...),
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    """O1: skip needs a lead second badge, always (regardless of whether the
    UI only offers Skip on required substeps -- server-side enforcement per
    contract §5, not trusting the client)."""
    try:
        lead = deps.second_badge(session, payload=override_badge, role="lead", actor=operator)
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/skip",
            lambda: substeps.skip_substep(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq, skip_reason=skip_reason, lead=lead,
            ),
        )
        session.commit()
    except (substeps.SubstepError, service.SecondBadgeError) as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/measurement")
def station_substep_measurement(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    value: str = Form(...),
    gauge_id: str = Form(""),
    notes: str = Form(""),
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    try:
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/measurement",
            lambda: substeps.record_measurement(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq, value=value,
                gauge_id=gauge_id or None, notes=notes or None,
            ),
        )
        session.commit()
    except substeps.SubstepError as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/disposition")
def station_substep_disposition(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    disposition: str = Form(...),
    failure_code_id: uuid.UUID = Form(...),
    rework_to_op_seq: int | None = Form(None),
    notes: str = Form(""),
    override_badge: str = Form(""),
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    """§4 O3 (use_as_is: lead or quality) / O4 (scrap: lead) / O5
    (rework_to_op: lead). `rework_in_place` needs no second badge."""
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
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/disposition",
            lambda: substeps.apply_disposition(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq, disposition=disposition,
                failure_code_id=failure_code_id, rework_to_op_seq=rework_to_op_seq,
                notes=notes or None, authorizer=authorizer,
            ),
        )
        session.commit()
    except (substeps.SubstepError, service.SecondBadgeError) as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/signoff")
def station_substep_signoff(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    override_badge: str = Form(...),
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    try:
        frozen_sub = substeps.peek_frozen_substep(session, unit_id, step_seq, substep_seq)
        role = frozen_sub.get("signoff_role") or "lead"
        authorizer = deps.second_badge(session, payload=override_badge, role=role, actor=operator)
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/signoff",
            lambda: substeps.complete_signoff(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq, authorizer=authorizer,
            ),
        )
        session.commit()
    except (substeps.SubstepError, service.SecondBadgeError) as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/material")
def station_substep_material(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    part_number: str = Form(...),
    qty_used: str = Form(...),
    qty_scrapped: str = Form("0"),
    lot: str = Form(""),
    uom: str = Form(""),
    substitution_for: str = Form(""),
    override_badge: str = Form(""),
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    try:
        authorizer = None
        if substitution_for:
            if not override_badge:
                raise service.SecondBadgeError(
                    "material substitution requires a lead badge scan"
                )
            authorizer = deps.second_badge(
                session, payload=override_badge, role="lead", actor=operator
            )
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/material",
            lambda: substeps.record_material(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq, part_number=part_number,
                qty_used=qty_used, qty_scrapped=qty_scrapped or "0", lot=lot or None,
                uom=uom or None, substitution_for=substitution_for or None, authorizer=authorizer,
            ),
        )
        session.commit()
    except (substeps.SubstepError, service.SecondBadgeError) as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


@router.post("/units/{unit_id}/substeps/{step_seq}/{substep_seq}/photo")
async def station_substep_photo(
    unit_id: uuid.UUID,
    step_seq: int,
    substep_seq: int,
    file: UploadFile,
    request_id: str | None = Form(None),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    ext = ALLOWED_CONTENT_TYPES.get(file.content_type)
    if ext is None:
        raise HTTPException(
            status_code=415, detail=f"unsupported content type '{file.content_type}'"
        )
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds upload limit")

    try:
        statemachine.with_request_dedup(
            session, request_id, "POST /station/substeps/photo",
            lambda: substeps.attach_photo(
                session, station=station, operator=operator, unit_id=unit_id,
                step_seq=step_seq, substep_seq=substep_seq, data=data, ext=ext,
            ),
        )
        session.commit()
    except substeps.SubstepError as exc:
        session.rollback()
        return _back(unit_id, step_seq, str(exc))
    return _back(unit_id, step_seq)


# -- session pause/resume/clock-out (footer controls) ---------------------------
# These call the existing app.domain.statemachine session primitives directly
# (P3-04, already implemented) -- no new domain logic needed.


def _own_open_session(
    session: Session, work_session_id: uuid.UUID, operator: Operator
) -> WorkSession:
    ws = session.get(WorkSession, work_session_id)
    if ws is None or ws.operator_id != operator.id or ws.ended_at is not None:
        raise HTTPException(status_code=404, detail="no matching open session for this operator")
    return ws


@router.post("/sessions/{work_session_id}/pause")
def station_session_pause(
    work_session_id: uuid.UUID,
    reason_code: str = Form(...),
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    ws = _own_open_session(session, work_session_id, operator)
    try:
        statemachine.pause_session(session, ws, reason_code=reason_code, actor_id=operator.id)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return RedirectResponse(
            f"/station/execute/{ws.unit_id}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/station/execute/{ws.unit_id}", status_code=303)


@router.post("/sessions/{work_session_id}/resume")
def station_session_resume(
    work_session_id: uuid.UUID,
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    ws = _own_open_session(session, work_session_id, operator)
    statemachine.resume_session(session, ws, actor_id=operator.id)
    session.commit()
    return RedirectResponse(f"/station/execute/{ws.unit_id}", status_code=303)


@router.post("/sessions/{work_session_id}/clock-out")
def station_session_clock_out(
    work_session_id: uuid.UUID,
    station: Station = Depends(deps.require_station),
    operator: Operator = Depends(deps.require_operator),
    session: Session = Depends(get_session),
):
    ws = _own_open_session(session, work_session_id, operator)
    statemachine.close_session(session, ws, reason="clocked_out", actor_id=operator.id)
    session.commit()
    return RedirectResponse("/station", status_code=303)
