"""Integration tests for order ingestion -> WorkOrder + Units + frozen
ExecutionPlan (P2-10, DD §4.3/§5/§6.1-6.2/§17.4-17.5).

Two layers, per the task brief:
  - `test_fixture_order_creates_workorder_via_sync_cycle`: the *full* pipeline
    -- fake-JB2 -> sync worker -> `app.sync.hooks` -> `workorders
    .create_from_line_item` -- proving the wiring end to end (P2-10 AC:
    "fixture order lands with frozen plan <= next sync cycle").
  - Everything else: domain-level, calling `app.domain.workorders` directly
    against hand-seeded mirror rows -- faster and more direct for exercising
    each ingestion/reconciliation rule in isolation.

DB is sqlite in-memory (StaticPool, one shared connection) -- same pattern
as tests/integration/test_sync.py / tests/unit/test_library_versioning.py.
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
from app.domain.models_jb2 import (
    Base,
    JB2Order,
    JB2OrderLineItem,
    JB2OrderRouting,
    JB2Outbox,
    MappingException,
)
from app.domain.models_library import InstructionSet, Product, ProductPartMap, Step, Substep

# -- shared fixtures ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _generous_backfill_window(monkeypatch):
    """Same rationale as tests/integration/test_sync.py: the fixture order
    below uses a fixed date rather than one relative to the real wall
    clock -- widen the first-run backfill window so it's never excluded."""
    import dataclasses

    from app.config import config
    from app.sync import checkpoints

    monkeypatch.setattr(
        checkpoints, "config", dataclasses.replace(config, sync_backfill_days=36500)
    )


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


def _seed_order(
    session: Session, *, jb2_id: str, order_number: str, status: str = "Open"
) -> JB2Order:
    order = JB2Order(
        id=uuid.uuid4(),
        jb2_id=jb2_id,
        order_number=order_number,
        customer="AGW Stock",
        status=status,
        due_date=None,
        **_mirror_common(),
    )
    session.add(order)
    session.flush()
    return order


def _seed_line_item(
    session: Session,
    *,
    jb2_id: str,
    part_number: str,
    qty: int,
    due_date: date | None = None,
    order_number: str | None = None,
    jb2_order_id: uuid.UUID | None = None,
) -> JB2OrderLineItem:
    payload = {"orderNumber": order_number} if order_number else {}
    li = JB2OrderLineItem(
        id=uuid.uuid4(),
        jb2_id=jb2_id,
        jb2_order_id=jb2_order_id,
        part_number=part_number,
        description=part_number,
        qty=qty,
        due_date=due_date,
        **{**_mirror_common(), "payload": payload},
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


def _apollo_product(session: Session) -> Product:
    product = Product(
        id=uuid.uuid4(),
        name="Apollo",
        variant_schema={"caliber": ["9mm", ".45"]},
        active=True,
    )
    session.add(product)
    session.flush()
    return product


def _map_part(session: Session, product: Product, part_number: str, caliber: str) -> ProductPartMap:
    m = ProductPartMap(
        id=uuid.uuid4(),
        product_id=product.id,
        jb2_part_number=part_number,
        variant_values={"caliber": caliber},
    )
    session.add(m)
    session.flush()
    return m


def _published_set_with_conditional_substep(session: Session, product: Product) -> InstructionSet:
    """OP10 -> one step, two substeps: one unconditional, one 9mm-only."""
    iset = library.create_set(
        session,
        product_id=product.id,
        title="Apollo — Slide Lightening Cuts",
        operation_match={"op_codes": ["OP10"], "work_centers": []},
        created_by="alice",
    )
    session.flush()
    step = library.add_step(session, iset.id, "Mill lightening cuts", seq=1, who="alice")
    library.add_substep(
        session, step.id, "action", "Load fixture", seq=1, who="alice"
    )
    library.add_substep(
        session,
        step.id,
        "measurement",
        "Verify 9mm chamber depth",
        seq=2,
        who="alice",
        condition={"field": "caliber", "in": ["9mm"]},
        measurement_spec={"name": "chamber_depth", "unit": "in", "nominal": 0.5,
                           "tol_plus": 0.01, "tol_minus": 0.01},
    )
    session.flush()
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "bob")
    session.flush()
    return iset


# -- 1. full pipeline: fake-JB2 -> sync -> hooks -> WorkOrder -----------------


def test_fixture_order_creates_workorder_via_sync_cycle(session, engine):
    """P2-10 AC: a fixture Apollo order lands as a work order with a frozen
    plan by the next sync cycle. Exercises the real wiring
    (app.sync.worker's order-hook mechanism + app.sync.hooks), not just the
    domain function directly."""
    from app.sync import hooks as sync_hooks
    from app.sync.worker import REGISTRY, _order_hooks, run_cycle

    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    _published_set_with_conditional_substep(session, product)
    session.commit()

    _order_hooks.append(sync_hooks.handle_order_event)
    try:
        state = {
            "orders": [
                {
                    "orderNumber": "10100",
                    "customerCode": "AGW",
                    "customerDescription": "AGW Stock",
                    "status": "Open",
                    "uniqueID": 1,
                    "lastModDate": "2026-06-01T00:00:00Z",
                }
            ],
            "order-line-items": [
                {
                    "orderNumber": "10100",
                    "jobNumber": "J-10100-1",
                    "itemNumber": 1,
                    "partNumber": "APOLLO-9-BLK",
                    "partDescription": "Apollo 9mm",
                    "quantityToMake": 1,
                    "dueDate": "2026-08-01T00:00:00Z",
                    "status": "Open",
                    "uniqueID": 2,
                    "lastModDate": "2026-06-01T00:00:00Z",
                }
            ],
            "order-routings": [
                {
                    "stepNumber": 10,
                    "operationCode": "OP10",
                    "description": "CNC Slide",
                    "workCenter": "CNC1",
                    "totalEstimatedHours": 1.5,
                    "uniqueID": 3,
                    "lastModDate": "2026-06-01T00:00:00Z",
                    "jobNumber": "J-10100-1",
                    "orderNumber": "10100",
                }
            ],
            "job-materials": [],
            "job-requirements": [],
        }

        class _FakeClient:
            """ponytail: minimal field[op]= filter support (eq/gte only --
            all worker.py's REGISTRY/child-fetch calls ever use), just
            enough to drive one sync cycle without the full ASGI fake-JB2
            app -- this test only needs one order/line-item/routing page,
            not JB2's pagination/error-injection surface."""

            def get(self, endpoint, params=None, fields=None, take=None):
                key = endpoint.strip("/")
                records = list(state.get(key, []))
                for raw_key, value in (params or {}).items():
                    if "[" in raw_key:
                        field, op = raw_key[:-1].split("[")
                    else:
                        field, op = raw_key, "eq"
                    if op == "gte":
                        records = [r for r in records if str(r.get(field, "")) >= str(value)]
                    else:
                        records = [r for r in records if str(r.get(field)) == str(value)]
                return records

            def iter_pages(self, endpoint, params, fields=None):
                yield self.get(endpoint, params=params, fields=fields)

        client = _FakeClient()
        orders_def = next(rd for rd in REGISTRY if rd.name == "orders")
        line_items_def = next(rd for rd in REGISTRY if rd.name == "order-line-items")

        run_cycle(session, client, orders_def)
        session.commit()
        run_cycle(session, client, line_items_def)
        session.commit()
    finally:
        _order_hooks.remove(sync_hooks.handle_order_event)

    wo = session.scalars(select(WorkOrder)).one()
    assert wo.status == "ready"
    assert wo.qty == 1
    units = session.scalars(select(Unit).where(Unit.work_order_id == wo.id)).all()
    assert len(units) == 1
    assert units[0].serial_number is None
    plan_ops = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == wo.id)
    ).all()
    assert len(plan_ops) == 1
    assert plan_ops[0].blocked is False
    assert plan_ops[0].frozen_content["steps"][0]["substeps"][0]["title"] == "Load fixture"


# -- 2. freeze invariant -------------------------------------------------------


def test_freeze_invariant_survives_new_published_version(session):
    """Publishing v2 of the bound instruction set must not change one byte
    of the already-frozen plan_operations.frozen_content (hash-compared)."""
    import copy
    import hashlib
    import json

    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    iset = _published_set_with_conditional_substep(session, product)
    session.commit()

    li = _seed_line_item(session, jb2_id="li-1", part_number="APOLLO-9-BLK", qty=1)
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    session.commit()

    wo = workorders.create_from_line_item(session, li)
    session.commit()

    plan_op = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == wo.id)
    ).one()
    before = copy.deepcopy(plan_op.frozen_content)
    before_hash = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()

    # Publish v2: fork a new draft, tweak a substep title, publish it.
    draft = library.new_draft_from(session, iset.id, "carol")
    session.flush()
    step2 = session.scalars(select(Step).where(Step.instruction_set_id == draft.id)).one()
    sub2 = session.scalars(
        select(Substep).where(Substep.step_id == step2.id, Substep.seq == 1)
    ).one()
    library.update_substep(session, sub2.id, title="Load fixture (v2 wording)")
    library.submit_for_review(session, draft.id)
    library.publish(session, draft.id, "dave")
    session.commit()

    session.refresh(plan_op)
    after_hash = hashlib.sha256(
        json.dumps(plan_op.frozen_content, sort_keys=True).encode()
    ).hexdigest()
    assert after_hash == before_hash
    assert plan_op.frozen_content["steps"][0]["substeps"][0]["title"] == "Load fixture"


# -- 3. conditional substep filtered by variant --------------------------------


def test_conditional_substep_present_for_9mm_absent_for_45(session):
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    _map_part(session, product, "APOLLO-45-BLK", ".45")
    _published_set_with_conditional_substep(session, product)
    session.commit()

    li_9mm = _seed_line_item(session, jb2_id="li-9mm", part_number="APOLLO-9-BLK", qty=1)
    _seed_routing(session, line_item_id=li_9mm.id, seq=10, operation_code="OP10")
    li_45 = _seed_line_item(session, jb2_id="li-45", part_number="APOLLO-45-BLK", qty=1)
    _seed_routing(session, line_item_id=li_45.id, seq=10, operation_code="OP10")
    session.commit()

    wo_9mm = workorders.create_from_line_item(session, li_9mm)
    wo_45 = workorders.create_from_line_item(session, li_45)
    session.commit()

    op_9mm = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == wo_9mm.id)
    ).one()
    op_45 = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == wo_45.id)
    ).one()

    titles_9mm = [s["title"] for s in op_9mm.frozen_content["steps"][0]["substeps"]]
    titles_45 = [s["title"] for s in op_45.frozen_content["steps"][0]["substeps"]]
    assert "Verify 9mm chamber depth" in titles_9mm
    assert "Verify 9mm chamber depth" not in titles_45
    assert "Load fixture" in titles_9mm and "Load fixture" in titles_45


# -- 4. unmapped part -> mapping_exception, no WorkOrder -----------------------


def test_unmapped_part_creates_mapping_exception_no_workorder(session):
    li = _seed_line_item(session, jb2_id="li-unmapped", part_number="MYSTERY-PART", qty=1)
    session.commit()

    result = workorders.create_from_line_item(session, li)
    session.commit()

    assert result is None
    assert session.scalars(select(WorkOrder)).first() is None
    exc = session.scalars(
        select(MappingException).where(MappingException.value == "MYSTERY-PART")
    ).one()
    assert exc.kind == "part_number"
    assert exc.resolved is False

    # Re-running for the same still-unmapped part must not spam duplicates.
    workorders.create_from_line_item(session, li)
    session.commit()
    count = len(
        session.scalars(
            select(MappingException).where(MappingException.value == "MYSTERY-PART")
        ).all()
    )
    assert count == 1


# -- 5. unbound routing step -> blocked_no_instructions ------------------------


def test_unbound_routing_step_blocks_workorder(session):
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    _published_set_with_conditional_substep(session, product)  # only binds OP10
    session.commit()

    li = _seed_line_item(session, jb2_id="li-unbound", part_number="APOLLO-9-BLK", qty=1)
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    _seed_routing(
        session, line_item_id=li.id, seq=20, operation_code="OP99", description="Mystery Op"
    )
    session.commit()

    wo = workorders.create_from_line_item(session, li)
    session.commit()

    assert wo.status == "blocked_no_instructions"
    plan_ops = session.scalars(
        select(PlanOperation)
        .where(PlanOperation.work_order_id == wo.id)
        .order_by(PlanOperation.seq)
    ).all()
    assert plan_ops[0].blocked is False
    assert plan_ops[1].blocked is True
    assert plan_ops[1].frozen_content == {"blocked": True, "title": "no instructions — see lead"}


# -- 6. qty increase adds units ------------------------------------------------


def test_qty_increase_adds_units(session):
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    _published_set_with_conditional_substep(session, product)
    session.commit()

    li = _seed_line_item(session, jb2_id="li-qty", part_number="APOLLO-9-BLK", qty=2)
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    session.commit()

    wo = workorders.create_from_line_item(session, li)
    session.commit()
    assert wo.qty == 2
    assert len(session.scalars(select(Unit).where(Unit.work_order_id == wo.id)).all()) == 2

    li.qty = 5
    session.commit()
    wo2 = workorders.create_from_line_item(session, li)  # idempotent -> reconcile
    session.commit()

    assert wo2.id == wo.id
    units = session.scalars(
        select(Unit).where(Unit.work_order_id == wo.id).order_by(Unit.unit_no)
    ).all()
    assert wo2.qty == 5
    assert [u.unit_no for u in units] == [1, 2, 3, 4, 5]
    assert all(u.serial_number is None for u in units)


# -- 7. cancel before start -> cancelled ---------------------------------------


def test_cancel_before_start_is_cancelled(session):
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    _published_set_with_conditional_substep(session, product)
    session.commit()

    li = _seed_line_item(session, jb2_id="li-cancel", part_number="APOLLO-9-BLK", qty=1)
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    session.commit()
    wo = workorders.create_from_line_item(session, li)
    session.commit()

    workorders.cancel_work_order(session, wo)
    session.commit()
    assert wo.status == "cancelled"


def test_cancel_after_start_is_cancel_requested(session):
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    _published_set_with_conditional_substep(session, product)
    session.commit()

    li = _seed_line_item(session, jb2_id="li-cancel2", part_number="APOLLO-9-BLK", qty=1)
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    session.commit()
    wo = workorders.create_from_line_item(session, li)
    session.commit()

    unit = session.scalars(select(Unit).where(Unit.work_order_id == wo.id)).one()
    unit.status = "at_station"
    session.commit()

    workorders.cancel_work_order(session, wo)
    session.commit()
    assert wo.status == "cancel_requested"


def test_order_closed_hook_cancels_work_orders_for_the_order(session):
    from app.sync import hooks as sync_hooks

    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    _published_set_with_conditional_substep(session, product)
    session.commit()

    order = _seed_order(session, jb2_id="order-10200", order_number="10200")
    li = _seed_line_item(
        session, jb2_id="li-order-closed", part_number="APOLLO-9-BLK", qty=1,
        order_number="10200", jb2_order_id=order.id,
    )
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    session.commit()
    wo = workorders.create_from_line_item(session, li)
    session.commit()
    assert wo.status == "ready"

    sync_hooks._handle_order_closed(session, {"orderNumber": "10200", "uniqueID": 99})
    session.commit()

    session.refresh(wo)
    assert wo.status == "cancelled"


# -- 8. routing drift ----------------------------------------------------------


def test_routing_change_after_release_flags_drift(session):
    product = _apollo_product(session)
    _map_part(session, product, "APOLLO-9-BLK", "9mm")
    _published_set_with_conditional_substep(session, product)
    session.commit()

    li = _seed_line_item(session, jb2_id="li-drift", part_number="APOLLO-9-BLK", qty=1)
    _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
    session.commit()
    wo = workorders.create_from_line_item(session, li)
    session.commit()
    assert wo.status == "ready"

    before = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == wo.id)
    ).one()
    before_content = before.frozen_content

    # JB2 changes the routing (new op code at seq 10) after release.
    routing = session.scalars(
        select(JB2OrderRouting).where(JB2OrderRouting.jb2_line_item_id == li.id)
    ).one()
    routing.operation_code = "OP15"
    session.commit()

    wo2 = workorders.create_from_line_item(session, li)  # idempotent -> reconcile
    session.commit()

    assert wo2.status == "routing_drift"
    # Never silently mutated (§17.5) -- the frozen plan is untouched.
    after = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == wo.id)
    ).one()
    assert after.frozen_content == before_content
    assert after.operation_code == "OP10"


# -- 9. outbox FK now enforced (Postgres only; sqlite has no FK enforcement) --


def test_outbox_work_order_fk_column_present():
    """The FK is real (see tests/unit/test_models_match_migration.py for the
    schema-level assertion); sqlite doesn't enforce FK constraints by
    default so a bogus insert wouldn't fail here the way it would on
    Postgres -- this just proves the happy path (a real work_order_id)
    round-trips."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        product = _apollo_product(session)
        _map_part(session, product, "APOLLO-9-BLK", "9mm")
        _published_set_with_conditional_substep(session, product)
        session.commit()

        li = _seed_line_item(session, jb2_id="li-outbox", part_number="APOLLO-9-BLK", qty=1)
        _seed_routing(session, line_item_id=li.id, seq=10, operation_code="OP10")
        session.commit()
        wo = workorders.create_from_line_item(session, li)
        session.commit()

        row = JB2Outbox(
            id=uuid.uuid4(),
            kind="time_ticket_detail",
            payload={},
            idempotency_key=f"wo:{wo.id}:op:10:finish",
            work_order_id=wo.id,
        )
        session.add(row)
        session.commit()

        fetched = session.get(JB2Outbox, row.id)
        assert fetched.work_order_id == wo.id
