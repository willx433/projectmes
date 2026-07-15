"""Instruction library builder UI (P2-04/P2-05) — DD §7.2, CR-003 (HTMX+Alpine).

Server-rendered PRG (post/redirect/get), same shape as app/api/products.py —
htmx is used only via `hx-boost` on the tree editor so tree edits (add/move/
delete step or substep) swap in place instead of a full page reload; every
route still works with plain form POSTs (hx-boost degrades to normal
navigation, and that's exactly what the integration tests exercise). Alpine
handles the substep-type-dependent field show/hide in the add-substep form —
no client-side state worth a JS build chain.

All state-machine/reorder/mutation rules live in app/domain/library.py; this
module only parses forms, calls those helpers, and turns LibraryError /
ConditionError into an inline `?error=` banner (base.html already renders
`error` from the template context).
"""
from __future__ import annotations

import json
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT, config
from app.db import get_session
from app.domain import library
from app.domain.conditions import ConditionError
from app.domain.conditions import validate as validate_condition
from app.domain.models_library import InstructionSet, Product, Step, Substep
from app.domain.sanitize import sanitize_html

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

SUBSTEP_TYPES = ("action", "measurement", "inspection", "photo", "material", "signoff")


# -- shared helpers -----------------------------------------------------------


def _redirect_error(url: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"{url}?error={quote(message)}", status_code=303)


def _parse_json_object(raw: str, label: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise library.LibraryError(f"{label}: invalid JSON ({exc})")
    if not isinstance(value, dict):
        raise library.LibraryError(f"{label}: must be a JSON object")
    return value


def _parse_condition(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise library.LibraryError(f"condition: invalid JSON ({exc})")
    if not isinstance(value, dict):
        raise library.LibraryError("condition: must be a JSON object")
    return value


def _parse_measurement_spec(form: dict) -> dict:
    """name/unit/nominal/+tol/-tol/gauge id/decimal places (DD §7.2). Numeric
    fields must actually parse as numbers and tolerances must carry the
    conventional sign (tol_plus >= 0, tol_minus <= 0) — the one bit of
    validation logic this screen owns that isn't already in app/domain/*."""
    name = (form.get("meas_name") or "").strip()
    if not name:
        raise library.LibraryError("measurement: name is required")

    def _num(key: str, label: str) -> float:
        raw = (form.get(key) or "").strip()
        if not raw:
            raise library.LibraryError(f"measurement: {label} is required")
        try:
            return float(raw)
        except ValueError:
            raise library.LibraryError(f"measurement: {label} must be numeric")

    nominal = _num("meas_nominal", "nominal")
    tol_plus = _num("meas_tol_plus", "+tol")
    tol_minus = _num("meas_tol_minus", "-tol")
    if tol_plus < 0:
        raise library.LibraryError("measurement: +tol must be >= 0")
    if tol_minus > 0:
        raise library.LibraryError("measurement: -tol must be <= 0")

    raw_decimals = (form.get("meas_decimals") or "0").strip()
    try:
        decimals = int(raw_decimals)
    except ValueError:
        raise library.LibraryError("measurement: decimal places must be an integer")
    if decimals < 0:
        raise library.LibraryError("measurement: decimal places must be >= 0")

    return {
        "name": name,
        "unit": (form.get("meas_unit") or "").strip(),
        "nominal": nominal,
        "tol_plus": tol_plus,
        "tol_minus": tol_minus,
        "gauge_id": (form.get("meas_gauge_id") or "").strip() or None,
        "decimals": decimals,
    }


def _get_set_or_404(session: Session, set_id: uuid.UUID) -> InstructionSet:
    iset = session.get(InstructionSet, set_id)
    if iset is None:
        raise library.LibraryError(f"instruction set '{set_id}' not found")
    return iset


def _tree(session: Session, set_id: uuid.UUID) -> list[dict]:
    steps = session.scalars(
        select(Step).where(Step.instruction_set_id == set_id).order_by(Step.seq)
    ).all()
    out = []
    for step in steps:
        substeps = session.scalars(
            select(Substep).where(Substep.step_id == step.id).order_by(Substep.seq)
        ).all()
        out.append({"step": step, "substeps": substeps})
    return out


def _sets_by_product(session: Session) -> tuple[list[Product], dict, list[InstructionSet]]:
    products = session.scalars(select(Product).order_by(Product.name)).all()
    all_sets = session.scalars(
        select(InstructionSet).order_by(InstructionSet.title, InstructionSet.version)
    ).all()
    by_product: dict = {}
    global_sets: list[InstructionSet] = []
    for iset in all_sets:
        if iset.product_id is None:
            global_sets.append(iset)
        else:
            by_product.setdefault(iset.product_id, []).append(iset)
    return products, by_product, global_sets


# -- library index --------------------------------------------------------


@router.get("/admin/library")
def admin_library_index(request: Request, session: Session = Depends(get_session)):
    products, by_product, global_sets = _sets_by_product(session)
    return templates.TemplateResponse(
        request,
        "library/index.html",
        {
            "products": products,
            "sets_by_product": by_product,
            "global_sets": global_sets,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/admin/library")
def admin_library_create_set(
    title: str = Form(...),
    product_id: str = Form(""),
    operation_match: str = Form(""),
    est_minutes: str = Form(""),
    who: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        op_match = _parse_json_object(operation_match, "operation_match")
        iset = library.create_set(
            session,
            product_id=uuid.UUID(product_id) if product_id.strip() else None,
            title=title,
            operation_match=op_match,
            created_by=who.strip() or None,
            est_minutes=int(est_minutes) if est_minutes.strip() else None,
        )
        session.commit()
    except (library.LibraryError, ValueError) as exc:
        session.rollback()
        return _redirect_error("/admin/library", str(exc))
    return RedirectResponse(f"/admin/library/sets/{iset.id}", status_code=303)


# -- set detail / tree editor -----------------------------------------------


@router.get("/admin/library/sets/{set_id}")
def admin_library_set_detail(
    set_id: uuid.UUID, request: Request, session: Session = Depends(get_session)
):
    iset = _get_set_or_404(session, set_id)
    product = session.get(Product, iset.product_id) if iset.product_id else None
    other_versions = session.scalars(
        select(InstructionSet)
        .where(InstructionSet.title == iset.title, InstructionSet.product_id == iset.product_id)
        .order_by(InstructionSet.version)
    ).all()
    products = session.scalars(select(Product).order_by(Product.name)).all()
    return templates.TemplateResponse(
        request,
        "library/set_detail.html",
        {
            "iset": iset,
            "product": product,
            "tree": _tree(session, set_id),
            "other_versions": other_versions,
            "products": products,
            "substep_types": SUBSTEP_TYPES,
            "editable": iset.state in library.EDITABLE_STATES,
            "require_approver": config.library_require_approver,
            "error": request.query_params.get("error"),
        },
    )


def _back(set_id: uuid.UUID) -> str:
    return f"/admin/library/sets/{set_id}"


# -- lifecycle actions --------------------------------------------------------


@router.post("/admin/library/sets/{set_id}/submit")
def admin_library_submit(
    set_id: uuid.UUID, who: str = Form(""), session: Session = Depends(get_session)
):
    try:
        library.submit_for_review(session, set_id, by=who.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id), str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


@router.post("/admin/library/sets/{set_id}/publish")
def admin_library_publish(
    set_id: uuid.UUID,
    who: str = Form(""),
    approver: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        library.publish(session, set_id, who.strip() or None, approver=approver.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id), str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


@router.post("/admin/library/sets/{set_id}/retire")
def admin_library_retire(
    set_id: uuid.UUID, who: str = Form(""), session: Session = Depends(get_session)
):
    try:
        library.retire(session, set_id, by=who.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id), str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


@router.post("/admin/library/sets/{set_id}/new-draft")
def admin_library_new_draft(
    set_id: uuid.UUID, who: str = Form(""), session: Session = Depends(get_session)
):
    try:
        clone = library.new_draft_from(session, set_id, who.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id), str(exc))
    return RedirectResponse(_back(clone.id), status_code=303)


@router.post("/admin/library/sets/{set_id}/clone")
def admin_library_clone(
    set_id: uuid.UUID,
    target_product_id: str = Form(""),
    who: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        target = uuid.UUID(target_product_id) if target_product_id.strip() else None
        clone = library.clone_set(session, set_id, target, who.strip() or None)
        session.commit()
    except (library.LibraryError, ValueError) as exc:
        session.rollback()
        return _redirect_error(_back(set_id), str(exc))
    return RedirectResponse(_back(clone.id), status_code=303)


# -- step mutations ------------------------------------------------------


@router.post("/admin/library/sets/{set_id}/steps")
def admin_library_add_step(
    set_id: uuid.UUID,
    title: str = Form(...),
    body_html: str = Form(""),
    est_minutes: str = Form(""),
    who: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        library.add_step(
            session,
            set_id,
            title,
            body_html=sanitize_html(body_html) or None,
            est_minutes=int(est_minutes) if est_minutes.strip() else None,
            who=who.strip() or None,
        )
        session.commit()
    except (library.LibraryError, ValueError) as exc:
        session.rollback()
        return _redirect_error(_back(set_id), str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


def _step_set_id(session: Session, step_id: uuid.UUID) -> uuid.UUID | None:
    step = session.get(Step, step_id)
    return step.instruction_set_id if step else None


@router.post("/admin/library/steps/{step_id}/update")
def admin_library_update_step(
    step_id: uuid.UUID,
    title: str = Form(...),
    body_html: str = Form(""),
    est_minutes: str = Form(""),
    who: str = Form(""),
    session: Session = Depends(get_session),
):
    set_id = _step_set_id(session, step_id)
    try:
        library.update_step(
            session,
            step_id,
            title=title,
            body_html=sanitize_html(body_html) or None,
            est_minutes=int(est_minutes) if est_minutes.strip() else None,
            who=who.strip() or None,
        )
        session.commit()
    except (library.LibraryError, ValueError) as exc:
        session.rollback()
        return _redirect_error(_back(set_id) if set_id else "/admin/library", str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


@router.post("/admin/library/steps/{step_id}/delete")
def admin_library_delete_step(
    step_id: uuid.UUID, who: str = Form(""), session: Session = Depends(get_session)
):
    set_id = _step_set_id(session, step_id)
    try:
        library.delete_step(session, step_id, who=who.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id) if set_id else "/admin/library", str(exc))
    return RedirectResponse(_back(set_id) if set_id else "/admin/library", status_code=303)


@router.post("/admin/library/steps/{step_id}/move")
def admin_library_move_step(
    step_id: uuid.UUID,
    direction: str = Form(...),
    who: str = Form(""),
    session: Session = Depends(get_session),
):
    set_id = _step_set_id(session, step_id)
    try:
        library.move_step(session, step_id, direction, who=who.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id) if set_id else "/admin/library", str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


# -- substep mutations -----------------------------------------------------


def _substep_step_and_set(session: Session, substep_id: uuid.UUID):
    substep = session.get(Substep, substep_id)
    if substep is None:
        return None, None
    step = session.get(Step, substep.step_id)
    return step, (step.instruction_set_id if step else None)


@router.post("/admin/library/steps/{step_id}/substeps")
async def admin_library_add_substep(
    step_id: uuid.UUID, request: Request, session: Session = Depends(get_session)
):
    form = dict((await request.form()).items())
    set_id = _step_set_id(session, step_id)
    try:
        iset = _get_set_or_404(session, set_id) if set_id else None
        product = session.get(Product, iset.product_id) if iset and iset.product_id else None
        variant_schema = product.variant_schema if product else None

        sub_type = (form.get("type") or "").strip()
        if sub_type not in SUBSTEP_TYPES:
            raise library.LibraryError(f"unknown substep type '{sub_type}'")

        condition = _parse_condition(form.get("condition") or "")
        try:
            validate_condition(condition, variant_schema)
        except ConditionError as exc:
            raise library.LibraryError(f"condition: {exc}")

        measurement_spec = (
            _parse_measurement_spec(form) if sub_type == "measurement" else None
        )
        signoff_role = (
            (form.get("signoff_role") or "").strip() or None if sub_type == "signoff" else None
        )

        library.add_substep(
            session,
            step_id,
            sub_type,
            (form.get("title") or "").strip() or sub_type.title(),
            body_html=sanitize_html(form.get("body_html") or "") or None,
            required=form.get("required") == "on",
            condition=condition,
            measurement_spec=measurement_spec,
            signoff_role=signoff_role,
            who=(form.get("who") or "").strip() or None,
        )
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id) if set_id else "/admin/library", str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


@router.post("/admin/library/substeps/{substep_id}/update")
async def admin_library_update_substep(
    substep_id: uuid.UUID, request: Request, session: Session = Depends(get_session)
):
    form = dict((await request.form()).items())
    step, set_id = _substep_step_and_set(session, substep_id)
    try:
        substep = session.get(Substep, substep_id)
        if substep is None:
            raise library.LibraryError(f"substep '{substep_id}' not found")
        iset = _get_set_or_404(session, set_id) if set_id else None
        product = session.get(Product, iset.product_id) if iset and iset.product_id else None
        variant_schema = product.variant_schema if product else None

        condition = _parse_condition(form.get("condition") or "")
        try:
            validate_condition(condition, variant_schema)
        except ConditionError as exc:
            raise library.LibraryError(f"condition: {exc}")

        sub_type = substep.type
        measurement_spec = (
            _parse_measurement_spec(form) if sub_type == "measurement" else substep.measurement_spec
        )
        signoff_role = (
            (form.get("signoff_role") or "").strip() or None
            if sub_type == "signoff"
            else substep.signoff_role
        )

        library.update_substep(
            session,
            substep_id,
            title=(form.get("title") or "").strip() or substep.title,
            body_html=sanitize_html(form.get("body_html") or "") or None,
            required=form.get("required") == "on",
            condition=condition,
            measurement_spec=measurement_spec,
            signoff_role=signoff_role,
            who=(form.get("who") or "").strip() or None,
        )
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id) if set_id else "/admin/library", str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


@router.post("/admin/library/substeps/{substep_id}/delete")
def admin_library_delete_substep(
    substep_id: uuid.UUID, who: str = Form(""), session: Session = Depends(get_session)
):
    _, set_id = _substep_step_and_set(session, substep_id)
    try:
        library.delete_substep(session, substep_id, who=who.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id) if set_id else "/admin/library", str(exc))
    return RedirectResponse(_back(set_id) if set_id else "/admin/library", status_code=303)


@router.post("/admin/library/substeps/{substep_id}/move")
def admin_library_move_substep(
    substep_id: uuid.UUID,
    direction: str = Form(...),
    who: str = Form(""),
    session: Session = Depends(get_session),
):
    _, set_id = _substep_step_and_set(session, substep_id)
    try:
        library.move_substep(session, substep_id, direction, who=who.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id) if set_id else "/admin/library", str(exc))
    return RedirectResponse(_back(set_id), status_code=303)


# -- diff / preview ----------------------------------------------------------


@router.get("/admin/library/sets/{set_id}/diff/{other_id}")
def admin_library_diff(
    set_id: uuid.UUID,
    other_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
):
    a = _get_set_or_404(session, set_id)
    b = _get_set_or_404(session, other_id)
    diff = library.diff_versions(session, set_id, other_id)
    return templates.TemplateResponse(
        request, "library/diff.html", {"a": a, "b": b, "diff": diff}
    )


@router.get("/admin/library/sets/{set_id}/preview")
def admin_library_preview(
    set_id: uuid.UUID, request: Request, session: Session = Depends(get_session)
):
    iset = _get_set_or_404(session, set_id)
    return templates.TemplateResponse(
        request,
        "library/preview.html",
        {"iset": iset, "tree": _tree(session, set_id)},
    )
