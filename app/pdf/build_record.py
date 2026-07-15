"""Per-serial build-record PDF (P4-08) -- DD §18.7 (open question 7,
"recommend yes") + §9 (the Data Capture Catalog this assembles into one
document): cover, every operation's step/substep spec overlaid with this
unit's *actual* execution (measurements, timestamps, operators, photos,
signoffs), failures/dispositions per op, work-session times, and full
scan/transit/box history.

Reuses `app.pdf.guide`'s WeasyPrint pattern (standalone Jinja env, inline-
CSS-only template, no reference to static/app.css) but is otherwise
independent of it -- a build record shows *actual recorded values*, the
build guide (`templates/guide/build_guide.html`) shows a blank form to
fill in, so they cannot share a template.

PERSISTENCE DECISION (see task brief's "avoid a migration if possible"):
no new table, no `plan_pdfs` row. A completed Unit's execution rows never
change again once `unit.status == 'done'` (that's a terminal state per
`app.domain.statemachine`/`app.domain.boxes`), so unlike the build guide
(whose *plan* can legitimately be regenerated as a work order's routing
drifts, hence `plan_pdfs`'s immutable version-per-generation history)
there is nothing here to version: "regenerate" and "the one true record"
are the same operation. `generate_build_record` always re-renders from
current DB state and overwrites one fixed path,
`{ARTIFACT_DIR}/build-records/{serial-or-unit-id}/record-v1.pdf`.
Regeneration is idempotent (same DB state -> byte-identical PDF) and cheap
enough to redo on demand rather than cache-and-invalidate or add a table
just to track a sha256 nobody queries.

LAZY-GENERATION DECISION: nothing hooks the finish-operation code path
(`app/api/operations.py`, a concurrent P3-10 agent's file, out of scope
here) to auto-generate this the moment a unit flips to `done` -- that
hook would reach into a module owned elsewhere for this task. Instead the
file is produced the first time anyone asks for it, via
`POST /admin/units/{id}/build-record` (see `app/api/build_record.py`); the
download route 404s until that has run at least once.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.orm import Session
from weasyprint import HTML

from app.config import REPO_ROOT, config
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    Attachment,
    BoxAssignment,
    BuildBox,
    Failure,
    Measurement,
    Operator,
    Scan,
    SessionPause,
    Station,
    StepExecution,
    SubstepExecution,
    Transit,
    WorkSession,
)
from app.domain.models_jb2 import JB2OrderLineItem
from app.domain.models_library import Product

_env = Environment(
    loader=FileSystemLoader(str(REPO_ROOT / "templates")),
    autoescape=True,
)


def _artifact_root() -> Path:
    return Path(config.artifact_dir or "./artifacts")


def unit_key(unit: Unit) -> str:
    """Directory name for `unit`'s record -- serial number when the unit has
    one (the human-meaningful key), else the unit id.

    ponytail: serial numbers are free-text operator entry (CR-007's "the
    serial pre-exists, operator types it in") with no path-safe-character
    validation anywhere upstream -- collapse anything but
    `[A-Za-z0-9._-]` rather than trusting arbitrary text as a directory
    name. The download route also re-validates via the usual
    resolve()-under-base traversal guard, so this is defense in depth, not
    the only guard.
    """
    raw = unit.serial_number or str(unit.id)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")
    return safe or str(unit.id)


def record_path(unit: Unit) -> Path:
    return _artifact_root() / "build-records" / unit_key(unit) / "record-v1.pdf"


def _operator_name(session: Session, operator_id: uuid.UUID | None) -> str | None:
    if operator_id is None:
        return None
    op = session.get(Operator, operator_id)
    return op.display_name if op else None


def _station_name(session: Session, station_id: uuid.UUID | None) -> str | None:
    if station_id is None:
        return None
    st = session.get(Station, station_id)
    return st.name if st else None


def _photo_uri(attachment: Attachment) -> str | None:
    """Absolute `file://` URI so WeasyPrint embeds the image straight off
    disk -- sidesteps needing `attachment.path` (artifact-dir-relative) to
    resolve against the *templates* directory `base_url`, which is what
    `templates/partials/instruction_steps.html`'s web-style `/artifacts/...`
    URLs rely on instead (fine for that HTML preview page, not for a
    standalone WeasyPrint render with no running server behind it)."""
    p = (_artifact_root() / attachment.path).resolve()
    return p.as_uri() if p.is_file() else None


def _op_entries(session: Session, unit: Unit, plan_ops: list[PlanOperation]) -> list[dict]:
    """One entry per plan_operation: frozen step/substep spec (same
    `frozen_content` source `app.pdf.guide` reads) overlaid with this
    unit's actual execution rows -- the part a build guide never shows
    (it's a blank form)."""
    entries = []
    for op in plan_ops:
        step_execs: dict[int, StepExecution] = {
            se.step_seq: se
            for se in session.scalars(
                select(StepExecution).where(
                    StepExecution.plan_operation_id == op.id,
                    StepExecution.unit_id == unit.id,
                    StepExecution.superseded.is_(False),
                )
            )
        }
        sub_execs_by_step: dict[int, dict[int, SubstepExecution]] = {}
        sub_exec_ids: list[uuid.UUID] = []
        for step_seq, se in step_execs.items():
            subs = {
                sub.substep_seq: sub
                for sub in session.scalars(
                    select(SubstepExecution).where(
                        SubstepExecution.step_execution_id == se.id,
                        SubstepExecution.superseded.is_(False),
                    )
                )
            }
            sub_execs_by_step[step_seq] = subs
            sub_exec_ids.extend(s.id for s in subs.values())

        measurements_by_sub: dict[uuid.UUID, list[Measurement]] = {}
        photos_by_sub: dict[uuid.UUID, list[dict]] = {}
        if sub_exec_ids:
            for m in session.scalars(
                select(Measurement).where(Measurement.substep_execution_id.in_(sub_exec_ids))
            ):
                measurements_by_sub.setdefault(m.substep_execution_id, []).append(m)
            for a in session.scalars(
                select(Attachment).where(
                    Attachment.entity_kind == "substep_execution",
                    Attachment.entity_id.in_(sub_exec_ids),
                    Attachment.kind == "photo",
                )
            ):
                uri = _photo_uri(a)
                if uri:
                    photos_by_sub.setdefault(a.entity_id, []).append(
                        {"uri": uri, "uploaded_by": _operator_name(session, a.uploaded_by)}
                    )

        steps = []
        for frozen_step in sorted(
            (op.frozen_content or {}).get("steps", []), key=lambda s: s["seq"]
        ):
            se = step_execs.get(frozen_step["seq"])
            substeps = []
            for frozen_sub in sorted(frozen_step.get("substeps", []), key=lambda s: s["seq"]):
                sub = sub_execs_by_step.get(frozen_step["seq"], {}).get(frozen_sub["seq"])
                substeps.append(
                    {
                        "seq": frozen_sub["seq"],
                        "title": frozen_sub["title"],
                        "type": frozen_sub["type"],
                        "required": frozen_sub.get("required", True),
                        "status": sub.status if sub else "pending",
                        "started_at": sub.started_at if sub else None,
                        "completed_at": sub.completed_at if sub else None,
                        "operator": _operator_name(session, sub.operator_id) if sub else None,
                        "notes": sub.notes if sub else None,
                        "skip_reason": sub.skip_reason if sub else None,
                        "disposition": sub.disposition if sub else None,
                        "disposition_by": (
                            _operator_name(session, sub.disposition_by) if sub else None
                        ),
                        "signoff_role": frozen_sub.get("signoff_role"),
                        "measurements": measurements_by_sub.get(sub.id, []) if sub else [],
                        "photos": photos_by_sub.get(sub.id, []) if sub else [],
                    }
                )
            steps.append(
                {
                    "seq": frozen_step["seq"],
                    "title": frozen_step["title"],
                    "status": se.status if se else "pending",
                    "started_at": se.started_at if se else None,
                    "completed_at": se.completed_at if se else None,
                    "completed_by": _operator_name(session, se.completed_by) if se else None,
                    "substeps": substeps,
                }
            )

        failure_rows = []
        for f in session.scalars(
            select(Failure)
            .where(Failure.unit_id == unit.id, Failure.plan_operation_id == op.id)
            .order_by(Failure.detected_at)
        ):
            rework_op = session.get(PlanOperation, f.rework_to_op) if f.rework_to_op else None
            failure_rows.append(
                {
                    "narrative": f.narrative,
                    "detected_by": _operator_name(session, f.detected_by),
                    "detected_at": f.detected_at,
                    "disposition": f.disposition,
                    "authorized_by": _operator_name(session, f.authorized_by),
                    "rework_to_op_title": rework_op.title if rework_op else None,
                }
            )

        entries.append({"op": op, "steps": steps, "failures": failure_rows})
    return entries


def _session_rows(
    session: Session, unit: Unit, plan_ops_by_id: dict[uuid.UUID, PlanOperation]
) -> tuple[list[dict], list[uuid.UUID]]:
    work_sessions = list(
        session.scalars(
            select(WorkSession)
            .where(WorkSession.unit_id == unit.id)
            .order_by(WorkSession.started_at)
        )
    )
    rows = []
    for ws in work_sessions:
        paused_seconds = 0.0
        for pause in session.scalars(
            select(SessionPause).where(SessionPause.work_session_id == ws.id)
        ):
            end = pause.ended_at or ws.ended_at
            if end is not None:
                paused_seconds += (end - pause.started_at).total_seconds()
        total_seconds = (
            (ws.ended_at - ws.started_at).total_seconds() if ws.ended_at else None
        )
        active_minutes = (
            round((total_seconds - paused_seconds) / 60, 1) if total_seconds is not None else None
        )
        op = plan_ops_by_id.get(ws.plan_operation_id)
        rows.append(
            {
                "op_title": op.title if op else None,
                "operator": _operator_name(session, ws.operator_id),
                "station": _station_name(session, ws.station_id),
                "kind": ws.kind,
                "started_at": ws.started_at,
                "ended_at": ws.ended_at,
                "close_reason": ws.close_reason,
                "active_minutes": active_minutes,
            }
        )
    return rows, [ws.id for ws in work_sessions]


def _scan_rows(session: Session, work_session_ids: list[uuid.UUID]) -> list[dict]:
    """ponytail: scoped to scans tied to one of this unit's work sessions.
    `scans.box_id` alone can't be attributed to a single unit -- a box is
    reused across units over its lifetime (DD §17.6) -- so a scan with no
    `work_session_id` (e.g. a reject before any session existed on this
    unit) isn't unit-attributable and is out of scope for a per-serial
    record. Upgrade path if that gap matters: walk `box_assignments` for
    this unit's bound windows and include box-level scans inside them."""
    if not work_session_ids:
        return []
    rows = []
    for s in session.scalars(
        select(Scan).where(Scan.work_session_id.in_(work_session_ids)).order_by(Scan.scanned_at)
    ):
        rows.append(
            {
                "scanned_at": s.scanned_at,
                "result": s.result,
                "raw_payload": s.raw_payload,
                "station": _station_name(session, s.station_id),
                "operator": _operator_name(session, s.operator_id),
                "override_by": _operator_name(session, s.override_by),
            }
        )
    return rows


def _transit_rows(session: Session, unit: Unit) -> list[dict]:
    rows = []
    for t in session.scalars(
        select(Transit).where(Transit.unit_id == unit.id).order_by(Transit.departed_at)
    ):
        rows.append(
            {
                "from_station": _station_name(session, t.from_station_id),
                "to_station": _station_name(session, t.to_station_id),
                "departed_at": t.departed_at,
                "arrived_at": t.arrived_at,
                "seconds": t.seconds,
            }
        )
    return rows


def _box_rows(session: Session, unit: Unit) -> list[dict]:
    rows = []
    for ba in session.scalars(
        select(BoxAssignment)
        .where(BoxAssignment.unit_id == unit.id)
        .order_by(BoxAssignment.assigned_at)
    ):
        box = session.get(BuildBox, ba.box_id)
        rows.append(
            {
                "label": (box.label or box.qr_payload) if box else None,
                "assigned_at": ba.assigned_at,
                "released_at": ba.released_at,
                "assigned_by": ba.assigned_by,
            }
        )
    return rows


def build_record_context(session: Session, unit: Unit, generated_by: str | None) -> dict:
    """Everything `templates/guide/build_record.html` needs, split out so
    tests can render the HTML directly without invoking WeasyPrint (same
    trick `tests/integration/test_pdf.py` uses on `guide_module
    ._op_render_entries` + a manual template render)."""
    work_order = session.get(WorkOrder, unit.work_order_id)
    product = session.get(Product, work_order.product_id) if work_order else None
    line_item = (
        session.get(JB2OrderLineItem, work_order.jb2_line_item_id) if work_order else None
    )
    payload = (line_item.payload or {}) if line_item else {}
    order_number = payload.get("orderNumber")
    job_number = payload.get("jobNumber")

    plan_ops = list(
        session.scalars(
            select(PlanOperation)
            .where(PlanOperation.work_order_id == unit.work_order_id)
            .order_by(PlanOperation.seq)
        )
    )
    plan_ops_by_id = {op.id: op for op in plan_ops}

    remake_of_serial = None
    if unit.remake_of_unit_id:
        original = session.get(Unit, unit.remake_of_unit_id)
        remake_of_serial = original.serial_number if original else None

    session_rows, ws_ids = _session_rows(session, unit, plan_ops_by_id)

    return {
        "unit": unit,
        "work_order": work_order,
        "product": product,
        "order_number": order_number,
        "job_number": job_number,
        "remake_of_serial": remake_of_serial,
        "ops": _op_entries(session, unit, plan_ops),
        "sessions": session_rows,
        "scans": _scan_rows(session, ws_ids),
        "transits": _transit_rows(session, unit),
        "boxes": _box_rows(session, unit),
        "generated_by": generated_by,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
    }


def generate_build_record(session: Session, unit: Unit, generated_by: str | None) -> Path:
    """Render + write `unit`'s build record, overwriting the same fixed
    path every call (see module docstring for why there's no version
    counter / persisted row here, unlike `app.pdf.guide`)."""
    ctx = build_record_context(session, unit, generated_by)
    html_str = _env.get_template("guide/build_record.html").render(**ctx)
    pdf_bytes = HTML(string=html_str, base_url=str(REPO_ROOT / "templates")).write_pdf()

    out_path = record_path(unit)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(pdf_bytes)
    return out_path
