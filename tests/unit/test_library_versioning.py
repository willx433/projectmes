"""Unit tests for app/domain/library.py (P2-03 / P2-08 service layer).

DB is sqlite in-memory (StaticPool, one connection) — same pattern as
tests/integration/test_outbox.py. Only the Library tables + jb2_reason_codes
are created (no full migration needed for a domain-logic unit test).
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.domain import library
from app.domain.models_jb2 import JB2ReasonCode
from app.domain.models_library import FailureCode, InstructionSet, Product, Step, Substep


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (Product, InstructionSet, Step, Substep, FailureCode, JB2ReasonCode):
        model.__table__.create(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _make_set(session, **overrides):
    kwargs = dict(
        product_id=None,
        title="Apollo — Slide Lightening Cuts",
        operation_match={"op_codes": ["OP10"], "work_centers": ["CNC1"]},
        created_by="alice",
    )
    kwargs.update(overrides)
    iset = library.create_set(session, **kwargs)
    session.flush()
    return iset


def _add_step_with_substep(session, set_id, seq=1, title="Step 1", who="alice"):
    step = library.add_step(session, set_id, title, seq=seq, who=who)
    library.add_substep(session, step.id, "action", f"{title} substep", seq=1, who=who)
    session.flush()
    return step


# --------------------------------------------------------------------
# Lifecycle: legal + illegal transitions
# --------------------------------------------------------------------


def test_create_set_starts_draft(session):
    iset = _make_set(session)
    assert iset.state == "draft"
    assert iset.version == 1


def test_submit_for_review_legal(session):
    iset = _make_set(session)
    library.submit_for_review(session, iset.id, by="alice")
    assert iset.state == "in_review"


def test_submit_for_review_illegal_from_non_draft(session):
    iset = _make_set(session)
    library.submit_for_review(session, iset.id)
    with pytest.raises(library.LibraryError):
        library.submit_for_review(session, iset.id)


def test_publish_illegal_from_draft(session):
    iset = _make_set(session)
    with pytest.raises(library.LibraryError):
        library.publish(session, iset.id, "bob")


def test_publish_legal_from_in_review(session):
    iset = _make_set(session)
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    assert iset.state == "published"
    assert iset.published_by == "bob"
    assert iset.published_at is not None


def test_retire_illegal_from_draft(session):
    iset = _make_set(session)
    with pytest.raises(library.LibraryError):
        library.retire(session, iset.id)


def test_retire_legal_from_published(session):
    iset = _make_set(session)
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    library.retire(session, iset.id)
    assert iset.state == "retired"


def test_retire_illegal_from_retired(session):
    iset = _make_set(session)
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    library.retire(session, iset.id)
    with pytest.raises(library.LibraryError):
        library.retire(session, iset.id)


# --------------------------------------------------------------------
# Publish retires the older published version of the same title
# --------------------------------------------------------------------


def test_publish_retires_older_published_same_title(session):
    v1 = _make_set(session)
    library.submit_for_review(session, v1.id)
    library.publish(session, v1.id, "bob")

    v2 = library.new_draft_from(session, v1.id, "alice")
    library.submit_for_review(session, v2.id)
    library.publish(session, v2.id, "bob")

    assert v1.state == "retired"
    assert v2.state == "published"


def test_publish_does_not_retire_different_title(session):
    v1 = _make_set(session, title="Set A")
    other = _make_set(session, title="Set B")
    library.submit_for_review(session, v1.id)
    library.publish(session, v1.id, "bob")
    library.submit_for_review(session, other.id)
    library.publish(session, other.id, "bob")

    assert v1.state == "published"
    assert other.state == "published"


# --------------------------------------------------------------------
# Edit-published-forks-draft
# --------------------------------------------------------------------


def test_edit_published_set_raises(session):
    iset = _make_set(session)
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    with pytest.raises(library.LibraryError):
        library.add_step(session, iset.id, "New step")


def test_edit_retired_set_raises(session):
    iset = _make_set(session)
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    library.retire(session, iset.id)
    with pytest.raises(library.LibraryError):
        library.add_step(session, iset.id, "New step")


def test_edit_draft_and_in_review_allowed(session):
    iset = _make_set(session)
    library.add_step(session, iset.id, "Step while draft")
    library.submit_for_review(session, iset.id)
    library.add_step(session, iset.id, "Step while in_review", seq=2)


def test_new_draft_from_requires_published(session):
    iset = _make_set(session)
    with pytest.raises(library.LibraryError):
        library.new_draft_from(session, iset.id, "alice")


def test_new_draft_from_deep_copies_and_leaves_original_untouched(session):
    v1 = _make_set(session)
    _add_step_with_substep(session, v1.id, seq=1, title="Step 1")
    library.submit_for_review(session, v1.id)
    library.publish(session, v1.id, "bob")

    v2 = library.new_draft_from(session, v1.id, "carol")
    assert v2.version == 2
    assert v2.parent_version_id == v1.id
    assert v2.state == "draft"

    v2_step = session.scalars(select(Step).where(Step.instruction_set_id == v2.id)).first()
    library.update_step(session, v2_step.id, title="Mutated title")

    v1_step = session.scalars(select(Step).where(Step.instruction_set_id == v1.id)).first()
    assert v1_step.title == "Step 1"  # original untouched


# --------------------------------------------------------------------
# Approver flag (LIBRARY_REQUIRE_APPROVER)
# --------------------------------------------------------------------


def _set_require_approver(monkeypatch, value):
    monkeypatch.setattr(
        library, "config", dataclasses.replace(library.config, library_require_approver=value)
    )


def test_approver_not_required_by_default(session, monkeypatch):
    _set_require_approver(monkeypatch, False)
    iset = _make_set(session)
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")  # no approver passed, still fine
    assert iset.state == "published"


def test_approver_required_when_flag_true(session, monkeypatch):
    _set_require_approver(monkeypatch, True)
    iset = _make_set(session, created_by="alice")
    library.submit_for_review(session, iset.id)
    with pytest.raises(library.LibraryError):
        library.publish(session, iset.id, "bob")  # no approver


def test_approver_must_differ_from_author(session, monkeypatch):
    _set_require_approver(monkeypatch, True)
    iset = _make_set(session, created_by="alice")
    library.submit_for_review(session, iset.id)
    with pytest.raises(library.LibraryError):
        library.publish(session, iset.id, "bob", approver="alice")


def test_approver_flag_true_accepts_different_approver(session, monkeypatch):
    _set_require_approver(monkeypatch, True)
    iset = _make_set(session, created_by="alice")
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob", approver="dave")
    assert iset.state == "published"


# --------------------------------------------------------------------
# resolve_published: product-over-global, op_code-over-work_center
# --------------------------------------------------------------------


def _published(session, **overrides):
    iset = _make_set(session, **overrides)
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    return iset


def test_resolve_published_product_over_global(session):
    product_id = uuid.uuid4()
    global_set = _published(
        session, title="Global Cerakote", operation_match={"op_codes": ["OP50"], "work_centers": []}
    )
    product_set = _published(
        session,
        product_id=product_id,
        title="Apollo Cerakote",
        operation_match={"op_codes": ["OP50"], "work_centers": []},
    )
    resolved = library.resolve_published(session, product_id, op_code="OP50")
    assert resolved.id == product_set.id

    # a product with no matching product-scoped set falls back to global
    other_product = uuid.uuid4()
    resolved2 = library.resolve_published(session, other_product, op_code="OP50")
    assert resolved2.id == global_set.id


def test_resolve_published_op_code_over_work_center(session):
    product_id = uuid.uuid4()
    by_wc = _published(
        session,
        product_id=product_id,
        title="By work center",
        operation_match={"op_codes": [], "work_centers": ["CNC1"]},
    )
    by_op = _published(
        session,
        product_id=product_id,
        title="By op code",
        operation_match={"op_codes": ["OP10"], "work_centers": []},
    )
    resolved = library.resolve_published(session, product_id, op_code="OP10", work_center="CNC1")
    assert resolved.id == by_op.id

    resolved_wc_only = library.resolve_published(session, product_id, work_center="CNC1")
    assert resolved_wc_only.id == by_wc.id


def test_resolve_published_no_match_returns_none(session):
    assert library.resolve_published(session, uuid.uuid4(), op_code="NOPE") is None


# --------------------------------------------------------------------
# diff_versions
# --------------------------------------------------------------------


def _get_step(session, set_id, seq):
    return session.scalars(
        select(Step).where(Step.instruction_set_id == set_id, Step.seq == seq)
    ).first()


def test_diff_versions_add_remove_change(session):
    v1 = _make_set(session, title="Diff test")
    _add_step_with_substep(session, v1.id, seq=1, title="Step 1")
    _add_step_with_substep(session, v1.id, seq=2, title="Step to remove")
    library.submit_for_review(session, v1.id)
    library.publish(session, v1.id, "bob")

    v2 = library.new_draft_from(session, v1.id, "carol")

    # change step 1's title
    v2_step1 = _get_step(session, v2.id, 1)
    library.update_step(session, v2_step1.id, title="Step 1 changed")

    # remove step 2 by deleting it directly (no delete helper requested;
    # simulate via ORM delete — still respects the editable-state rule via
    # the fixture already being draft)
    v2_step2 = _get_step(session, v2.id, 2)
    session.delete(session.get(Step, v2_step2.id))

    # add a new step 3 (explicit seq — step 2's seq was just freed by the
    # delete above and would otherwise be recycled, masking the remove)
    library.add_step(session, v2.id, "Step 3 new", seq=3)
    session.flush()

    diff = library.diff_versions(session, v1.id, v2.id)

    assert any(s["seq"] == 1 for s in diff["steps"]["changed"])
    assert any(s["seq"] == 2 for s in diff["steps"]["removed"])
    assert any(s["seq"] == 3 for s in diff["steps"]["added"])


def test_diff_versions_substep_change(session):
    v1 = _make_set(session, title="Substep diff")
    _add_step_with_substep(session, v1.id, seq=1, title="Step 1")
    library.submit_for_review(session, v1.id)
    library.publish(session, v1.id, "bob")

    v2 = library.new_draft_from(session, v1.id, "carol")
    v2_step = _get_step(session, v2.id, 1)
    v2_sub = session.scalars(select(Substep).where(Substep.step_id == v2_step.id)).first()
    library.update_substep(session, v2_sub.id, title="Mutated substep title")

    diff = library.diff_versions(session, v1.id, v2.id)
    assert any(s["seq"] == 1 for s in diff["substeps"]["changed"])


# --------------------------------------------------------------------
# clone_set
# --------------------------------------------------------------------


def test_clone_set_produces_independent_draft(session):
    v1 = _make_set(session, title="Apollo Op40")
    _add_step_with_substep(session, v1.id, seq=1, title="Step 1")

    target_product = uuid.uuid4()
    clone = library.clone_set(session, v1.id, target_product, "carol")

    assert clone.product_id == target_product
    assert clone.version == 1
    assert clone.parent_version_id is None
    assert clone.state == "draft"

    clone_step = _get_step(session, clone.id, 1)
    library.update_step(session, clone_step.id, title="Cloned step edited")

    v1_step = _get_step(session, v1.id, 1)
    assert v1_step.title == "Step 1"


# --------------------------------------------------------------------
# Failure codes
# --------------------------------------------------------------------


def test_failure_code_product_shadows_global(session):
    library.upsert_failure_code(session, None, "SCRATCH", "Surface scratch")
    library.upsert_failure_code(session, None, "DENT", "Dent")
    product_id = uuid.uuid4()
    library.upsert_failure_code(session, product_id, "SCRATCH", "Apollo-specific scratch rule")

    codes = library.list_failure_codes(session, product_id)
    by_code = {c.code: c for c in codes}
    assert by_code["SCRATCH"].label == "Apollo-specific scratch rule"
    assert by_code["DENT"].label == "Dent"


def test_failure_code_unknown_reason_number_rejected(session):
    with pytest.raises(library.LibraryError):
        library.upsert_failure_code(session, None, "SCRAP1", "Scrap cause", jb2_reason_number=999)


def test_failure_code_known_reason_number_accepted(session):
    session.add(
        JB2ReasonCode(
            id=uuid.uuid4(),
            reason_number=7,
            description="Machining error",
            payload={},
            content_hash="x",
            synced_at=datetime.now(timezone.utc),
        )
    )
    session.flush()
    fc = library.upsert_failure_code(
        session, None, "MACH_ERR", "Machining error", jb2_reason_number=7
    )
    assert fc.jb2_reason_number == 7


def test_failure_code_none_reason_number_means_unmapped(session):
    fc = library.upsert_failure_code(session, None, "UNMAPPED1", "Not yet mapped")
    assert fc.jb2_reason_number is None
