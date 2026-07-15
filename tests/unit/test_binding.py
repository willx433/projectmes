"""Unit tests for app/domain/binding.py (P2-09 binding resolver, DD §6.2)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.domain import binding, library
from app.domain.models_library import InstructionSet, Product, Step, Substep


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (Product, InstructionSet, Step, Substep):
        model.__table__.create(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@dataclass
class FakeRoutingStep:
    operation_code: str | None = None
    work_center_code: str | None = None
    description: str | None = None


def _published_set(session, *, product_id=None, title, operation_match):
    iset = library.create_set(
        session,
        product_id=product_id,
        title=title,
        operation_match=operation_match,
        created_by="alice",
    )
    session.flush()
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    session.flush()
    return iset


PRODUCT_ID = uuid.uuid4()


def test_rung_a_exact_op_code(session):
    target = _published_set(
        session,
        product_id=PRODUCT_ID,
        title="Apollo — Slide Lightening Cuts",
        operation_match={"op_codes": ["OP10"], "work_centers": ["CNC1"]},
    )
    step = FakeRoutingStep(operation_code="OP10", work_center_code="CNC9", description="unrelated")
    result = binding.bind_routing_step(session, PRODUCT_ID, step)
    assert result.instruction_set.id == target.id
    assert result.rung == "op_code"
    assert not result.fuzzy
    assert not result.blocked


def test_rung_b_work_center_only_when_op_code_misses(session):
    target = _published_set(
        session,
        product_id=PRODUCT_ID,
        title="Apollo — Frame Fit",
        operation_match={"op_codes": ["OP20"], "work_centers": ["FIT1"]},
    )
    # op_code doesn't match anything, but work_center does
    step = FakeRoutingStep(operation_code="OP99", work_center_code="FIT1", description="unrelated")
    result = binding.bind_routing_step(session, PRODUCT_ID, step)
    assert result.instruction_set.id == target.id
    assert result.rung == "work_center"
    assert not result.fuzzy
    assert not result.blocked


def test_rung_b_not_used_when_op_code_matches(session):
    _published_set(
        session,
        product_id=PRODUCT_ID,
        title="Apollo — Barrel Fit",
        operation_match={"op_codes": ["OP30"], "work_centers": ["WRONG"]},
    )
    other = _published_set(
        session,
        product_id=PRODUCT_ID,
        title="Apollo — Other Op",
        operation_match={"op_codes": ["OP31"], "work_centers": ["RIGHT"]},
    )
    step = FakeRoutingStep(operation_code="OP31", work_center_code="RIGHT", description="x")
    result = binding.bind_routing_step(session, PRODUCT_ID, step)
    assert result.instruction_set.id == other.id
    assert result.rung == "op_code"


def test_rung_c_fuzzy_match_above_threshold(session):
    target = _published_set(
        session,
        product_id=PRODUCT_ID,
        title="Apollo — Final QC Inspection",
        operation_match={"op_codes": ["OP60"], "work_centers": ["QC1"]},
    )
    step = FakeRoutingStep(
        operation_code="OP61",
        work_center_code="NOPE",
        description="Apollo Final QC Inspect",  # close but not exact
    )
    result = binding.bind_routing_step(session, PRODUCT_ID, step)
    assert result.instruction_set.id == target.id
    assert result.rung == "fuzzy"
    assert result.fuzzy is True
    assert result.score >= binding.FUZZY_THRESHOLD


def test_rung_c_fuzzy_below_threshold_is_unbound(session):
    _published_set(
        session,
        product_id=PRODUCT_ID,
        title="Apollo — Final QC Inspection",
        operation_match={"op_codes": ["OP60"], "work_centers": ["QC1"]},
    )
    step = FakeRoutingStep(
        operation_code="OPXX",
        work_center_code="NOPE",
        description="Totally different unrelated text",
    )
    result = binding.bind_routing_step(session, PRODUCT_ID, step)
    assert result.instruction_set is None
    assert result.rung == "unbound"
    assert result.blocked is True
    assert not result.fuzzy


def test_rung_d_unbound_blocked_placeholder(session):
    step = FakeRoutingStep(operation_code="OPZZ", work_center_code="ZZ", description=None)
    result = binding.bind_routing_step(session, PRODUCT_ID, step)
    assert result.instruction_set is None
    assert result.blocked is True
    assert result.rung == "unbound"


def test_global_fallback_when_no_product_scoped_set(session):
    target = _published_set(
        session,
        product_id=None,
        title="Global — Deburr",
        operation_match={"op_codes": ["OP99"], "work_centers": ["DEBURR"]},
    )
    step = FakeRoutingStep(operation_code="OP99", work_center_code="DEBURR", description="x")
    result = binding.bind_routing_step(session, PRODUCT_ID, step)
    assert result.instruction_set.id == target.id


def test_product_scope_wins_over_global(session):
    global_set = _published_set(
        session,
        product_id=None,
        title="Global — Deburr",
        operation_match={"op_codes": ["OP99"]},
    )
    product_set = _published_set(
        session,
        product_id=PRODUCT_ID,
        title="Apollo — Deburr",
        operation_match={"op_codes": ["OP99"]},
    )
    step = FakeRoutingStep(operation_code="OP99", work_center_code=None, description="x")
    result = binding.bind_routing_step(session, PRODUCT_ID, step)
    assert result.instruction_set.id == product_set.id
    assert result.instruction_set.id != global_set.id


def test_bind_full_routing_summary(session):
    _published_set(
        session,
        product_id=PRODUCT_ID,
        title="Apollo — Slide Lightening Cuts",
        operation_match={"op_codes": ["OP10"]},
    )
    steps = [
        FakeRoutingStep(operation_code="OP10", work_center_code=None, description=None),
        FakeRoutingStep(operation_code="OPZZ", work_center_code=None, description=None),
    ]
    summary = binding.bind_full_routing(session, PRODUCT_ID, steps)
    assert len(summary.results) == 2
    assert summary.any_blocked is True
    assert summary.any_fuzzy is False
