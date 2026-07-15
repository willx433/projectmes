"""Sharper coverage of order-change reconciliation (P2-13, DD §17.4/17.5),
beyond the basics already covered in test_order_to_plan.py (qty increase,
routing drift happy path, cancel-before/after-start).

Calls `app.domain.workorders.reconcile_changes` directly against hand-seeded
mirror rows -- no fake-JB2 needed, same pattern as test_order_to_plan.py.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.domain import library, workorders
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting
from app.domain.models_library import InstructionSet, Product, ProductPartMap


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _mirror_common(now_hash: str = "h") -> dict:
    from datetime import datetime, timezone

    return {
        "payload": {},
        "content_hash": now_hash,
        "jb2_last_modified": None,
        "synced_at": datetime.now(timezone.utc),
    }


def _apollo_product(session: Session) -> Product:
    product = Product(id=uuid.uuid4(), name="Apollo", variant_schema={}, active=True)
    session.add(product)
    session.flush()
    return product


def _map_part(session: Session, product: Product, part_number: str) -> ProductPartMap:
    m = ProductPartMap(
        id=uuid.uuid4(), product_id=product.id, jb2_part_number=part_number, variant_values={}
    )
    session.add(m)
    session.flush()
    return m


def _published_set(session: Session, product: Product) -> InstructionSet:
    iset = library.create_set(
        session,
        product_id=product.id,
        title="Apollo — CNC Slide",
        operation_match={"op_codes": ["OP10"], "work_centers": []},
        created_by="alice",
    )
    session.flush()
    step = library.add_step(session, iset.id, "Mill", seq=1, who="alice")
    library.add_substep(session, step.id, "action", "Load fixture", seq=1, who="alice")
    session.flush()
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    session.flush()
    return iset


def _seed_line_item(
    session: Session, *, jb2_id: str, part_number: str, qty: int, due_date: date | None = None
) -> JB2OrderLineItem:
    li = JB2OrderLineItem(
        id=uuid.uuid4(),
        jb2_id=jb2_id,
        jb2_order_id=None,
        part_number=part_number,
        description=part_number,
        qty=qty,
        due_date=due_date,
        **_mirror_common(),
    )
    session.add(li)
    session.flush()
    return li


def _seed_routing(
    session: Session, *, line_item_id, seq: int, operation_code: str, description: str = ""
) -> JB2OrderRouting:
    routing = JB2OrderRouting(
        id=uuid.uuid4(),
        jb2_id=f"routing-{line_item_id}-{seq}",
        jb2_line_item_id=line_item_id,
        seq=seq,
        operation_code=operation_code,
        description=description or operation_code,
        work_center_code=None,
        est_setup_hrs=None,
        est_run_hrs=None,
        **_mirror_common(),
    )
    session.add(routing)
    session.flush()
    return routing


def _setup_wo(session: Session, *, qty: int = 3, due_date: date | None = None) -> WorkOrder:
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK")
    _published_set(session, product)
    session.commit()

    li = _seed_line_item(
        session, jb2_id="li-1", part_number="APOLLO-9-BLK", qty=qty, due_date=due_date
    )
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    session.commit()

    wo = workorders.create_from_line_item(session, li)
    session.commit()
    return wo


# -- qty decrease: flags notes, keeps status -----------------------------------


def test_qty_decrease_flags_notes_and_keeps_status(session):
    wo = _setup_wo(session, qty=3)
    assert wo.status == "ready"
    assert wo.notes is None

    li = session.scalars(select(JB2OrderLineItem)).one()
    li.qty = 1
    session.commit()

    wo2 = workorders.reconcile_changes(session, wo, li)
    session.commit()

    assert wo2.qty == 1
    assert wo2.status == "ready"  # unchanged -- lead disposition is a to-do, not an auto-cancel
    assert wo2.notes is not None
    assert "3 -> 1" in wo2.notes
    # no units removed -- decrease only flags, never silently cancels/deletes
    assert len(session.scalars(select(Unit).where(Unit.work_order_id == wo.id)).all()) == 3


# -- due-date change propagates ------------------------------------------------


def test_due_date_change_propagates_to_work_order(session):
    wo = _setup_wo(session, qty=1, due_date=date(2026, 8, 1))
    assert wo.due_date == date(2026, 8, 1)

    li = session.scalars(select(JB2OrderLineItem)).one()
    li.due_date = date(2026, 9, 15)
    session.commit()

    wo2 = workorders.reconcile_changes(session, wo, li)
    session.commit()

    assert wo2.due_date == date(2026, 9, 15)


# -- routing drift: sets status, never touches frozen_content bytes -----------


def test_routing_drift_sets_status_and_never_mutates_frozen_content(session):
    wo = _setup_wo(session, qty=1)
    plan_op = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == wo.id)
    ).one()
    before_content = plan_op.frozen_content
    before_op_code = plan_op.operation_code

    routing = session.scalars(select(JB2OrderRouting)).one()
    routing.operation_code = "OP99"
    session.commit()

    li = session.scalars(select(JB2OrderLineItem)).one()
    wo2 = workorders.reconcile_changes(session, wo, li)
    session.commit()

    assert wo2.status == "routing_drift"
    session.refresh(plan_op)
    assert plan_op.frozen_content == before_content
    assert plan_op.operation_code == before_op_code  # never silently rewritten (§17.5)


# -- cancel_requested when any unit not queued ---------------------------------


def test_cancel_requested_when_any_unit_not_queued(session):
    wo = _setup_wo(session, qty=2)
    unit = session.scalars(select(Unit).where(Unit.work_order_id == wo.id)).first()
    unit.status = "at_station"
    session.commit()

    workorders.cancel_work_order(session, wo)
    session.commit()

    assert wo.status == "cancel_requested"


def test_cancel_all_queued_is_cancelled_not_requested(session):
    wo = _setup_wo(session, qty=2)
    # all units still "queued" (default) -- straight cancel, no lead disposition needed
    workorders.cancel_work_order(session, wo)
    session.commit()

    assert wo.status == "cancelled"


# -- second reconcile call is idempotent ---------------------------------------


def test_second_reconcile_call_is_idempotent(session):
    wo = _setup_wo(session, qty=2, due_date=date(2026, 8, 1))
    li = session.scalars(select(JB2OrderLineItem)).one()
    li.qty = 5
    li.due_date = date(2026, 9, 1)
    session.commit()

    workorders.reconcile_changes(session, wo, li)
    session.commit()
    units_after_first = session.scalars(
        select(Unit).where(Unit.work_order_id == wo.id)
    ).all()
    assert len(units_after_first) == 5
    notes_after_first = wo.notes

    # calling again with the same (already-applied) line item state must not
    # add more units, duplicate the qty-change note, or re-flag anything.
    workorders.reconcile_changes(session, wo, li)
    session.commit()

    units_after_second = session.scalars(
        select(Unit).where(Unit.work_order_id == wo.id)
    ).all()
    assert len(units_after_second) == 5
    assert [u.unit_no for u in units_after_second] == [1, 2, 3, 4, 5]
    assert wo.notes == notes_after_first
    assert wo.due_date == date(2026, 9, 1)
    assert wo.qty == 5
