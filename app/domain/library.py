"""Instruction library domain service (DD §7: builder/versioning rules).

Lifecycle: ``draft -> in_review -> published -> retired`` (§7.1). Only
``published`` sets bind to new execution plans (P2-09); in-flight plans hold
frozen copies made at plan generation and are never touched here (Phase 2
later work, §7.3 "propagate to in-flight").

Publish semantics (§7.3, resolved per the P2-03 task brief): publishing a
version does **not** retroactively rewrite history — it marks *this* version
``published`` and retires any *other* currently-published version of the
same ``(product_id, title)`` lineage, so future binding always resolves to
exactly one published version per (product, title). Editing a published set
never mutates it in place — ``new_draft_from`` forks a new draft version
(v+1); mutation helpers reject any edit attempt on a non-draft/in_review set.

ponytail: one module of plain functions taking a `Session`, no
repository/service-object layer — this mirrors app/outbox and app/sync,
the existing domain-logic style in this repo.
"""
from __future__ import annotations

import copy
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import config
from app.domain.models_jb2 import JB2ReasonCode
from app.domain.models_library import FailureCode, InstructionSet, Step, Substep

logger = logging.getLogger("app.domain.library")

EDITABLE_STATES = {"draft", "in_review"}


class LibraryError(Exception):
    """Illegal state transition, illegal edit, or invalid reference."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_set(session: Session, set_id: uuid.UUID) -> InstructionSet:
    iset = session.get(InstructionSet, set_id)
    if iset is None:
        raise LibraryError(f"instruction set '{set_id}' not found")
    return iset


def _check_editable(iset: InstructionSet) -> None:
    if iset.state not in EDITABLE_STATES:
        raise LibraryError(
            f"instruction set '{iset.id}' is {iset.state}; edits are only "
            "allowed on draft/in_review sets"
        )


def _next_seq(session: Session, model: type, fk_column: Any, fk_value: Any) -> int:
    existing = session.scalars(select(model.seq).where(fk_column == fk_value)).all()
    return (max(existing) + 1) if existing else 1


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def create_set(
    session: Session,
    *,
    product_id: uuid.UUID | None,
    title: str,
    operation_match: dict,
    created_by: str | None,
    est_minutes: int | None = None,
) -> InstructionSet:
    iset = InstructionSet(
        id=uuid.uuid4(),
        product_id=product_id,
        operation_match=operation_match,
        title=title,
        state="draft",
        version=1,
        est_minutes=est_minutes,
        created_by=created_by,
    )
    session.add(iset)
    logger.info(
        "library_create_set",
        extra={"who": created_by, "set_id": str(iset.id), "title": title},
    )
    return iset


def submit_for_review(
    session: Session, set_id: uuid.UUID, *, by: str | None = None
) -> InstructionSet:
    iset = _get_set(session, set_id)
    if iset.state != "draft":
        raise LibraryError(
            f"cannot submit for review from state '{iset.state}' (must be draft)"
        )
    iset.state = "in_review"
    iset.updated_at = _utcnow()
    logger.info("library_submit_for_review", extra={"who": by, "set_id": str(iset.id)})
    return iset


def publish(
    session: Session,
    set_id: uuid.UUID,
    published_by: str | None,
    *,
    approver: str | None = None,
) -> InstructionSet:
    iset = _get_set(session, set_id)
    if iset.state != "in_review":
        raise LibraryError(
            f"cannot publish from state '{iset.state}' (must be in_review)"
        )
    if config.library_require_approver:
        if not approver:
            raise LibraryError(
                "approver required to publish (LIBRARY_REQUIRE_APPROVER=true)"
            )
        if approver == iset.created_by:
            raise LibraryError("approver must differ from the draft's author")

    now = _utcnow()
    iset.state = "published"
    iset.published_by = published_by
    iset.published_at = now
    iset.updated_at = now

    superseded = session.scalars(
        select(InstructionSet).where(
            InstructionSet.product_id == iset.product_id,
            InstructionSet.title == iset.title,
            InstructionSet.state == "published",
            InstructionSet.id != iset.id,
        )
    ).all()
    for old in superseded:
        old.state = "retired"
        old.updated_at = now
        logger.info(
            "library_retire_superseded",
            extra={
                "who": published_by,
                "set_id": str(old.id),
                "superseded_by": str(iset.id),
            },
        )

    logger.info(
        "library_publish",
        extra={
            "who": published_by,
            "approver": approver,
            "set_id": str(iset.id),
            "title": iset.title,
            "version": iset.version,
        },
    )
    return iset


def retire(session: Session, set_id: uuid.UUID, *, by: str | None = None) -> InstructionSet:
    iset = _get_set(session, set_id)
    if iset.state != "published":
        raise LibraryError(f"cannot retire from state '{iset.state}' (must be published)")
    iset.state = "retired"
    iset.updated_at = _utcnow()
    logger.info("library_retire", extra={"who": by, "set_id": str(iset.id)})
    return iset


# --------------------------------------------------------------------------
# Edit-published-forks-draft (§7.3)
# --------------------------------------------------------------------------


def _copy_steps(session: Session, src_set_id: uuid.UUID, dst_set_id: uuid.UUID) -> None:
    steps = session.scalars(
        select(Step).where(Step.instruction_set_id == src_set_id).order_by(Step.seq)
    ).all()
    for step in steps:
        new_step_id = uuid.uuid4()
        session.add(
            Step(
                id=new_step_id,
                instruction_set_id=dst_set_id,
                seq=step.seq,
                title=step.title,
                body_html=step.body_html,
                est_minutes=step.est_minutes,
            )
        )
        substeps = session.scalars(
            select(Substep).where(Substep.step_id == step.id).order_by(Substep.seq)
        ).all()
        for sub in substeps:
            session.add(
                Substep(
                    id=uuid.uuid4(),
                    step_id=new_step_id,
                    seq=sub.seq,
                    type=sub.type,
                    title=sub.title,
                    body_html=sub.body_html,
                    required=sub.required,
                    condition=copy.deepcopy(sub.condition),
                    measurement_spec=copy.deepcopy(sub.measurement_spec),
                    media=copy.deepcopy(sub.media),
                    signoff_role=sub.signoff_role,
                )
            )


def new_draft_from(
    session: Session, set_id: uuid.UUID, created_by: str | None
) -> InstructionSet:
    """Fork a new draft version (v+1) from a published set (§7.3). The
    source set is never mutated; steps/substeps are deep-copied."""
    source = _get_set(session, set_id)
    if source.state != "published":
        raise LibraryError(
            f"cannot fork a new draft from state '{source.state}' (must be published)"
        )
    new_id = uuid.uuid4()
    clone = InstructionSet(
        id=new_id,
        product_id=source.product_id,
        operation_match=copy.deepcopy(source.operation_match),
        title=source.title,
        state="draft",
        version=source.version + 1,
        parent_version_id=source.id,
        est_minutes=source.est_minutes,
        created_by=created_by,
    )
    session.add(clone)
    _copy_steps(session, source.id, new_id)
    logger.info(
        "library_new_draft_from",
        extra={
            "who": created_by,
            "source_set_id": str(source.id),
            "new_set_id": str(new_id),
        },
    )
    return clone


def clone_set(
    session: Session,
    set_id: uuid.UUID,
    target_product_id: uuid.UUID | None,
    created_by: str | None,
) -> InstructionSet:
    """Clone across products (§7.2): new independent draft v1 under
    ``target_product_id``, no lineage back to the source."""
    source = _get_set(session, set_id)
    new_id = uuid.uuid4()
    clone = InstructionSet(
        id=new_id,
        product_id=target_product_id,
        operation_match=copy.deepcopy(source.operation_match),
        title=source.title,
        state="draft",
        version=1,
        parent_version_id=None,
        est_minutes=source.est_minutes,
        created_by=created_by,
    )
    session.add(clone)
    _copy_steps(session, source.id, new_id)
    logger.info(
        "library_clone_set",
        extra={
            "who": created_by,
            "source_set_id": str(source.id),
            "new_set_id": str(new_id),
        },
    )
    return clone


# --------------------------------------------------------------------------
# Mutation helpers (draft/in_review only)
# --------------------------------------------------------------------------


def add_step(
    session: Session,
    instruction_set_id: uuid.UUID,
    title: str,
    *,
    body_html: str | None = None,
    est_minutes: int | None = None,
    seq: int | None = None,
    who: str | None = None,
) -> Step:
    iset = _get_set(session, instruction_set_id)
    _check_editable(iset)
    if seq is None:
        seq = _next_seq(session, Step, Step.instruction_set_id, instruction_set_id)
    step = Step(
        id=uuid.uuid4(),
        instruction_set_id=instruction_set_id,
        seq=seq,
        title=title,
        body_html=body_html,
        est_minutes=est_minutes,
    )
    session.add(step)
    logger.info(
        "library_add_step",
        extra={"who": who, "set_id": str(instruction_set_id), "step_id": str(step.id)},
    )
    return step


def update_step(
    session: Session, step_id: uuid.UUID, *, who: str | None = None, **fields: Any
) -> Step:
    step = session.get(Step, step_id)
    if step is None:
        raise LibraryError(f"step '{step_id}' not found")
    _check_editable(_get_set(session, step.instruction_set_id))
    for key, value in fields.items():
        setattr(step, key, value)
    logger.info(
        "library_update_step",
        extra={"who": who, "step_id": str(step_id), "fields": sorted(fields)},
    )
    return step


def delete_step(session: Session, step_id: uuid.UUID, *, who: str | None = None) -> None:
    """Flagged addition (P2-04): the tree editor needs step delete and the
    lifecycle module had no mutation-removal helper yet. Cascades substeps
    itself -- migration 0005 has no ON DELETE CASCADE (see its FK block)."""
    step = session.get(Step, step_id)
    if step is None:
        raise LibraryError(f"step '{step_id}' not found")
    _check_editable(_get_set(session, step.instruction_set_id))
    session.execute(delete(Substep).where(Substep.step_id == step_id))
    session.delete(step)
    logger.info(
        "library_delete_step", extra={"who": who, "step_id": str(step_id)}
    )


def move_step(
    session: Session, step_id: uuid.UUID, direction: str, *, who: str | None = None
) -> None:
    """Flagged addition (P2-04): swap this step's `seq` with its immediate
    neighbor ('up' or 'down') -- the up/down reorder buttons (P10-compliant
    choice over drag/drop, see task brief). Three-update swap because
    `uq_steps_instruction_set_seq` is checked immediately, not deferred."""
    if direction not in ("up", "down"):
        raise LibraryError(f"invalid direction '{direction}' (must be 'up' or 'down')")
    step = session.get(Step, step_id)
    if step is None:
        raise LibraryError(f"step '{step_id}' not found")
    iset = _get_set(session, step.instruction_set_id)
    _check_editable(iset)
    siblings = session.scalars(
        select(Step).where(Step.instruction_set_id == step.instruction_set_id).order_by(Step.seq)
    ).all()
    idx = next(i for i, s in enumerate(siblings) if s.id == step.id)
    neighbor_idx = idx - 1 if direction == "up" else idx + 1
    if neighbor_idx < 0 or neighbor_idx >= len(siblings):
        return  # already at the edge -- no-op, not an error
    neighbor = siblings[neighbor_idx]
    a_seq, b_seq = step.seq, neighbor.seq
    step.seq = -1  # placeholder clears the unique slot before the swap
    session.flush()
    neighbor.seq = a_seq
    session.flush()
    step.seq = b_seq
    logger.info(
        "library_move_step",
        extra={"who": who, "step_id": str(step_id), "direction": direction},
    )


def delete_substep(session: Session, substep_id: uuid.UUID, *, who: str | None = None) -> None:
    """Flagged addition (P2-04): mirrors `delete_step`."""
    substep = session.get(Substep, substep_id)
    if substep is None:
        raise LibraryError(f"substep '{substep_id}' not found")
    step = session.get(Step, substep.step_id)
    _check_editable(_get_set(session, step.instruction_set_id))
    session.delete(substep)
    logger.info(
        "library_delete_substep", extra={"who": who, "substep_id": str(substep_id)}
    )


def move_substep(
    session: Session, substep_id: uuid.UUID, direction: str, *, who: str | None = None
) -> None:
    """Flagged addition (P2-04): mirrors `move_step`, within the same step."""
    if direction not in ("up", "down"):
        raise LibraryError(f"invalid direction '{direction}' (must be 'up' or 'down')")
    substep = session.get(Substep, substep_id)
    if substep is None:
        raise LibraryError(f"substep '{substep_id}' not found")
    step = session.get(Step, substep.step_id)
    _check_editable(_get_set(session, step.instruction_set_id))
    siblings = session.scalars(
        select(Substep).where(Substep.step_id == substep.step_id).order_by(Substep.seq)
    ).all()
    idx = next(i for i, s in enumerate(siblings) if s.id == substep.id)
    neighbor_idx = idx - 1 if direction == "up" else idx + 1
    if neighbor_idx < 0 or neighbor_idx >= len(siblings):
        return
    neighbor = siblings[neighbor_idx]
    a_seq, b_seq = substep.seq, neighbor.seq
    substep.seq = -1  # park to dodge the (step_id, seq) unique constraint mid-swap
    session.flush()
    neighbor.seq = a_seq
    session.flush()
    substep.seq = b_seq
    logger.info(
        "library_move_substep",
        extra={"who": who, "substep_id": str(substep_id), "direction": direction},
    )


def add_substep(
    session: Session,
    step_id: uuid.UUID,
    type: str,
    title: str,
    *,
    body_html: str | None = None,
    required: bool = True,
    condition: dict | None = None,
    measurement_spec: dict | None = None,
    media: list | None = None,
    signoff_role: str | None = None,
    seq: int | None = None,
    who: str | None = None,
) -> Substep:
    step = session.get(Step, step_id)
    if step is None:
        raise LibraryError(f"step '{step_id}' not found")
    _check_editable(_get_set(session, step.instruction_set_id))
    if seq is None:
        seq = _next_seq(session, Substep, Substep.step_id, step_id)
    substep = Substep(
        id=uuid.uuid4(),
        step_id=step_id,
        seq=seq,
        type=type,
        title=title,
        body_html=body_html,
        required=required,
        condition=condition,
        measurement_spec=measurement_spec,
        media=media if media is not None else [],
        signoff_role=signoff_role,
    )
    session.add(substep)
    logger.info(
        "library_add_substep",
        extra={"who": who, "step_id": str(step_id), "substep_id": str(substep.id)},
    )
    return substep


def update_substep(
    session: Session, substep_id: uuid.UUID, *, who: str | None = None, **fields: Any
) -> Substep:
    substep = session.get(Substep, substep_id)
    if substep is None:
        raise LibraryError(f"substep '{substep_id}' not found")
    step = session.get(Step, substep.step_id)
    _check_editable(_get_set(session, step.instruction_set_id))
    for key, value in fields.items():
        setattr(substep, key, value)
    logger.info(
        "library_update_substep",
        extra={"who": who, "substep_id": str(substep_id), "fields": sorted(fields)},
    )
    return substep


def set_substep_media(
    session: Session, substep_id: uuid.UUID, media_list: list[dict], *, who: str | None = None
) -> Substep:
    """Flagged addition (P2-06, DD §7.2/C21): overwrite a substep's ``media``
    json (``[{kind,url,caption}]``). Thin wrapper over `update_substep` --
    same editable-state guard and logging, no new rule to duplicate. The
    router owns validating the shape of `media_list` before it gets here."""
    return update_substep(session, substep_id, media=media_list, who=who)


# --------------------------------------------------------------------------
# Binding support: global-vs-product override resolution (§6.2 rungs 1-2;
# fuzzy rung 3 and the unbound placeholder are P2-09's, not this module's)
# --------------------------------------------------------------------------


def resolve_published(
    session: Session,
    product_id: uuid.UUID | None,
    *,
    op_code: str | None = None,
    work_center: str | None = None,
) -> InstructionSet | None:
    """Product-scoped published set matching ``op_code``/``work_center``
    wins; else fall back to a global (``product_id`` NULL) match. Within
    each scope, an exact ``op_code`` match beats a ``work_center`` match."""
    scopes = [product_id] if product_id is None else [product_id, None]
    for scope in scopes:
        candidates = session.scalars(
            select(InstructionSet).where(
                InstructionSet.product_id == scope,
                InstructionSet.state == "published",
            )
        ).all()
        if op_code is not None:
            for c in candidates:
                if op_code in (c.operation_match.get("op_codes") or []):
                    return c
        if work_center is not None:
            for c in candidates:
                if work_center in (c.operation_match.get("work_centers") or []):
                    return c
    return None


# --------------------------------------------------------------------------
# Diff for publish review (no UI yet — P2-08 renders this)
# --------------------------------------------------------------------------

_STEP_FIELDS = ("title", "body_html", "est_minutes")
_SUBSTEP_FIELDS = (
    "type",
    "title",
    "body_html",
    "required",
    "condition",
    "measurement_spec",
    "signoff_role",
)


def _step_dict(step: Step) -> dict:
    return {"seq": step.seq, **{f: getattr(step, f) for f in _STEP_FIELDS}}


def _substep_dict(substep: Substep) -> dict:
    return {"seq": substep.seq, **{f: getattr(substep, f) for f in _SUBSTEP_FIELDS}}


def diff_versions(session: Session, a_id: uuid.UUID, b_id: uuid.UUID) -> dict:
    """Structured diff of two instruction-set versions, by seq, at both the
    step and substep level. Substeps are only diffed under steps present in
    both versions (a step add/remove already covers its own substeps)."""
    _get_set(session, a_id)
    _get_set(session, b_id)

    a_steps = {
        s.seq: s for s in session.scalars(select(Step).where(Step.instruction_set_id == a_id)).all()
    }
    b_steps = {
        s.seq: s for s in session.scalars(select(Step).where(Step.instruction_set_id == b_id)).all()
    }

    steps_added: list[dict] = []
    steps_removed: list[dict] = []
    steps_changed: list[dict] = []
    substeps_added: list[dict] = []
    substeps_removed: list[dict] = []
    substeps_changed: list[dict] = []

    for seq in sorted(set(a_steps) | set(b_steps)):
        sa, sb = a_steps.get(seq), b_steps.get(seq)
        if sa is None:
            steps_added.append(_step_dict(sb))
            continue
        if sb is None:
            steps_removed.append(_step_dict(sa))
            continue

        changed = [f for f in _STEP_FIELDS if getattr(sa, f) != getattr(sb, f)]
        if changed:
            steps_changed.append(
                {"seq": seq, "fields": changed, "before": _step_dict(sa), "after": _step_dict(sb)}
            )

        a_sub = {
            x.seq: x
            for x in session.scalars(select(Substep).where(Substep.step_id == sa.id)).all()
        }
        b_sub = {
            x.seq: x
            for x in session.scalars(select(Substep).where(Substep.step_id == sb.id)).all()
        }
        for sseq in sorted(set(a_sub) | set(b_sub)):
            ssa, ssb = a_sub.get(sseq), b_sub.get(sseq)
            if ssa is None:
                substeps_added.append({"step_seq": seq, **_substep_dict(ssb)})
                continue
            if ssb is None:
                substeps_removed.append({"step_seq": seq, **_substep_dict(ssa)})
                continue
            sub_changed = [f for f in _SUBSTEP_FIELDS if getattr(ssa, f) != getattr(ssb, f)]
            if sub_changed:
                substeps_changed.append(
                    {
                        "step_seq": seq,
                        "seq": sseq,
                        "fields": sub_changed,
                        "before": _substep_dict(ssa),
                        "after": _substep_dict(ssb),
                    }
                )

    return {
        "steps": {"added": steps_added, "removed": steps_removed, "changed": steps_changed},
        "substeps": {
            "added": substeps_added,
            "removed": substeps_removed,
            "changed": substeps_changed,
        },
    }


# --------------------------------------------------------------------------
# Failure-code taxonomy (§9.6 / §7.2)
# --------------------------------------------------------------------------


def upsert_failure_code(
    session: Session,
    product_id: uuid.UUID | None,
    code: str,
    label: str,
    *,
    category: str | None = None,
    jb2_reason_number: int | None = None,
    who: str | None = None,
) -> FailureCode:
    """``jb2_reason_number=None`` means explicitly unmapped (opt-out). If
    given, it must exist in the ``jb2_reason_codes`` mirror."""
    if jb2_reason_number is not None:
        known = session.scalar(
            select(JB2ReasonCode.id).where(JB2ReasonCode.reason_number == jb2_reason_number)
        )
        if known is None:
            raise LibraryError(
                f"jb2_reason_number {jb2_reason_number} is not a known JB2 reason code"
            )

    existing = session.scalar(
        select(FailureCode).where(FailureCode.product_id == product_id, FailureCode.code == code)
    )
    if existing is not None:
        existing.label = label
        existing.category = category
        existing.jb2_reason_number = jb2_reason_number
        fc = existing
    else:
        fc = FailureCode(
            id=uuid.uuid4(),
            product_id=product_id,
            code=code,
            label=label,
            category=category,
            jb2_reason_number=jb2_reason_number,
        )
        session.add(fc)

    logger.info(
        "library_upsert_failure_code",
        extra={
            "who": who,
            "product_id": str(product_id) if product_id else None,
            "code": code,
            "jb2_reason_number": jb2_reason_number,
        },
    )
    return fc


def list_failure_codes(session: Session, product_id: uuid.UUID | None) -> list[FailureCode]:
    """Product-scoped codes shadow a global code of the same ``code``."""
    product_codes = (
        session.scalars(select(FailureCode).where(FailureCode.product_id == product_id)).all()
        if product_id is not None
        else []
    )
    global_codes = session.scalars(
        select(FailureCode).where(FailureCode.product_id.is_(None))
    ).all()
    shadowed = {fc.code for fc in product_codes}
    result = list(product_codes) + [fc for fc in global_codes if fc.code not in shadowed]
    return sorted(result, key=lambda fc: fc.code)
