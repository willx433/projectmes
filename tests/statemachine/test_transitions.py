"""Property tests for P3-14, docs/state-machine.md §10 obligations (a)-(g).

Zero-network sqlite, domain-level seeding -- same conventions as
tests/integration/test_scan.py / test_finish_and_writeback.py /
test_station_flow.py (this task's required reading), but this file drives
the domain layer directly (app.domain.statemachine/substeps/failures/
sessions + app.api.operations.finish_operation called as a plain function,
bypassing FastAPI's Depends plumbing -- these ARE "the public API" per
state-machine.md's own preamble: "every floor endpoint calls
app/domain/statemachine.py ... No endpoint mutates state except through a
transition function listed here"). This is faster than HTTP+cookie auth for
hypothesis stateful fuzzing and matches the READ FIRST list's emphasis on
those functions specifically.

Obligation -> section map:
  (a)/(b)/(e) -- UnitWalkMachine, a hypothesis RuleBasedStateMachine walking
      one unit through a small plan via scan/substep/finish/rework calls.
  (b) also gets direct deterministic tests (double-finish, duplicate scan,
      request_id replay) since those are exact, cheap, and worth pinning down
      without relying on the fuzzer finding them.
  (c) -- role-gate property (hypothesis @given over role combinations) +
      deterministic O4/O5/O6 lead-requirement checks.
  (d) -- deterministic: out-of-tolerance measurement blocks finish until a
      disposition is applied.
  (f) -- deterministic: auto-closed session withholds its outbox row until
      O6 lead-confirm, then produces exactly one.
  (g) -- deterministic: each atomic transition helper (session
      open/pause/resume/close, substep complete/fail/skip) emits exactly one
      events row per call (and zero on an idempotent repeat); a separate
      test documents+guards the codebase's one deliberate exception (a
      compound call recording more than one distinct audited fact).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.operations import FinishBody, finish_operation
from app.auth import service
from app.config import config as real_config
from app.domain import failures, statemachine, substeps
from app.domain import sessions as sessions_domain
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    BoxAssignment,
    BuildBox,
    Event,
    Operator,
    SessionPause,
    Station,
    SubstepExecution,
    WorkSession,
)
from app.domain.models_jb2 import Base, JB2Employee, JB2OrderLineItem, JB2OrderRouting, JB2Outbox
from app.domain.models_library import FailureCode, Product

# Registers the full model graph on Base.metadata (work_orders etc.).
from app.main import app as _main_app  # noqa: F401

# -- shared seeding helpers (same pattern as the sibling integration test files) --


def _make_engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    # ponytail: same sqlite bigint-autoincrement workaround as test_scan.py.
    original_type = Event.__table__.c.id.type
    Event.__table__.c.id.type = Integer()
    try:
        Base.metadata.create_all(eng)
    finally:
        Event.__table__.c.id.type = original_type
    return eng


@pytest.fixture
def engine():
    eng = _make_engine()
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


def _mirror_common() -> dict:
    return {
        "payload": {}, "content_hash": "h", "jb2_last_modified": None,
        "synced_at": datetime.now(timezone.utc),
    }


def _sub(seq, type_, *, required=True, spec=None, signoff_role=None):
    return {
        "seq": seq, "type": type_, "title": f"sub{seq}", "body_html": None,
        "required": required, "measurement_spec": spec, "media": [], "signoff_role": signoff_role,
    }


def _seed_plan(
    db_session: Session,
    *,
    n_ops: int = 3,
    work_center: str = "CNC1",
    work_centers: tuple[str, ...] | None = None,
    substeps_for_op=None,
    job_number: str = "10008-01",
) -> tuple[WorkOrder, list[PlanOperation], Unit]:
    """One WorkOrder, PlanOperations (one per `work_centers` entry, else
    `n_ops` copies of `work_center`), one Unit (qty=1). `substeps_for_op(seq)`
    returns the frozen substep list for that op's single step -- defaults to
    one required action substep, enough to drive finish-gating."""
    wcs = work_centers or tuple([work_center] * n_ops)
    substeps_for_op = substeps_for_op or (lambda seq: [_sub(1, "action")])

    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=1,
        payload={"jobNumber": job_number, "orderNumber": "10008"},
        content_hash="h", jb2_last_modified=None, synced_at=datetime.now(timezone.utc),
    )
    db_session.add(line_item)
    db_session.flush()

    product = Product(id=uuid.uuid4(), name="Apollo", variant_schema={}, active=True)
    db_session.add(product)
    db_session.flush()

    work_order = WorkOrder(
        id=uuid.uuid4(), jb2_line_item_id=line_item.id, product_id=product.id, qty=1,
        status="in_progress",
    )
    db_session.add(work_order)
    db_session.flush()

    plan_ops = []
    for seq, wc in enumerate(wcs, start=1):
        routing = JB2OrderRouting(
            id=uuid.uuid4(), jb2_id=f"routing-{work_order.id}-{seq}",
            jb2_line_item_id=line_item.id, seq=seq, operation_code=f"OP{seq * 10}",
            description=f"Op {seq}", work_center_code=wc, **_mirror_common(),
        )
        db_session.add(routing)
        db_session.flush()

        plan_op = PlanOperation(
            id=uuid.uuid4(), work_order_id=work_order.id, seq=seq, jb2_routing_id=routing.id,
            operation_code=routing.operation_code, title=f"Op {seq}",
            frozen_content={
                "steps": [{"seq": 1, "title": "Step 1", "substeps": substeps_for_op(seq)}],
            },
            status="pending",
        )
        db_session.add(plan_op)
        plan_ops.append(plan_op)
    db_session.flush()

    unit = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=1, status="queued",
        first_pass=True, rework_count=0,
    )
    db_session.add(unit)
    db_session.flush()

    return work_order, plan_ops, unit


def _bind_box(db_session: Session, unit: Unit, *, payload: str | None = None) -> BuildBox:
    box = BuildBox(
        id=uuid.uuid4(), qr_payload=payload or f"BOX:{uuid.uuid4()}", current_unit_id=unit.id,
    )
    db_session.add(box)
    db_session.flush()
    db_session.add(BoxAssignment(id=uuid.uuid4(), box_id=box.id, unit_id=unit.id))
    db_session.flush()
    return box


def _make_station(db_session, *, name: str = "Station", work_center_code: str = "CNC1") -> Station:
    station, _token = service.create_station(
        db_session, name=name, work_center_code=work_center_code,
    )
    db_session.commit()
    return station


def _make_operator(db_session, *, name: str = "Operator", roles=None) -> Operator:
    operator = service.create_operator(db_session, display_name=name, roles=roles or ["operator"])
    db_session.commit()
    return operator


def _make_operator_with_employee(
    db_session, *, name="Alice", roles=None, employee_code="42",
) -> Operator:
    jb2_emp = JB2Employee(
        id=uuid.uuid4(), jb2_id=f"emp-{uuid.uuid4()}", employee_code=employee_code,
        name=name, active=True, **_mirror_common(),
    )
    db_session.add(jb2_emp)
    db_session.flush()
    operator = service.create_operator(
        db_session, display_name=name, roles=roles or ["operator"], jb2_employee_id=jb2_emp.id,
    )
    db_session.commit()
    return operator


def _mark_step_done(db_session: Session, unit: Unit, plan_op: PlanOperation, step_seq: int = 1):
    # ponytail: superseded must be set explicitly -- sqlite's NUMERIC-affinity
    # Boolean column reads server_default="false" back truthy on insert
    # (see test_finish_and_writeback.py's own note on this pre-existing quirk).
    from app.domain.models_floor import StepExecution

    step_exec = StepExecution(
        plan_operation_id=plan_op.id, unit_id=unit.id, step_seq=step_seq, status="done",
        started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
        superseded=False,
    )
    db_session.add(step_exec)
    db_session.commit()
    return step_exec


def _event_count(db_session: Session, verb: str | None = None) -> int:
    stmt = select(Event)
    if verb is not None:
        stmt = stmt.where(Event.verb == verb)
    return len(db_session.execute(stmt).scalars().all())


# =============================================================================
# (a) / (b) / (e) -- hypothesis stateful machine: one unit walking its plan
# =============================================================================


class UnitWalkMachine(RuleBasedStateMachine):
    """Models one unit walking a 3-op plan (all at the same work center, to
    keep the station side trivial) through scan/complete/finish/rework calls,
    and asserts the DB never diverges from the model's tracked position/
    status/first_pass/rework_count -- the concrete, checkable form of (a)
    "no illegal transition reachable" and (e) "rework resets K..N, first_pass
    never returns to True" (driven over several rework loops via repeated
    rule application within one hypothesis run)."""

    N_OPS = 3

    def __init__(self):
        super().__init__()
        self.engine = _make_engine()
        self.db = Session(self.engine)

        self.station = _make_station(self.db, work_center_code="CNC1")
        self.operator = _make_operator(self.db, name="Op")
        self.lead = _make_operator(self.db, name="Lead", roles=["operator", "lead"])
        _, self.plan_ops, self.unit = _seed_plan(
            self.db, n_ops=self.N_OPS, work_center="CNC1",
        )
        self.box = _bind_box(self.db, self.unit)
        self.failure_code = FailureCode(
            id=uuid.uuid4(), product_id=None, code="X", label="X", active=True,
        )
        self.db.add(self.failure_code)
        self.db.commit()

        # model state -- mirrors what the DB should show after every rule
        self.model_idx = 0
        self.model_status = "queued"
        self.model_session_open = False
        self.model_step_done = [False] * self.N_OPS
        self.model_first_pass = True
        self.model_rework_count = 0

    def teardown(self):
        self.db.close()
        self.engine.dispose()

    # -- rules ----------------------------------------------------------------

    def _scan(self):
        result = statemachine.resolve_scan(
            self.db, self.box.qr_payload, self.station, self.operator,
        )
        self.db.commit()
        return result

    @rule()
    def scan(self):
        if self.model_status in ("done", "scrapped"):
            result = self._scan()
            assert result.code == "unit_terminal"
            return

        result = self._scan()
        if not self.model_session_open:
            assert result.code == "accepted", result.code
            self.model_session_open = True
            self.model_status = "at_station"
        else:
            # S10 -- duplicate scan is idempotent, no second session
            assert result.code == "already_active", result.code

    @rule()
    def complete_current_step(self):
        if not self.model_session_open or self.model_status in ("done", "scrapped"):
            return
        if self.model_step_done[self.model_idx]:
            return
        substeps.complete_substep(
            self.db, station=self.station, operator=self.operator, unit_id=self.unit.id,
            step_seq=1, substep_seq=1,
        )
        self.db.commit()
        self.model_step_done[self.model_idx] = True

    @rule()
    def finish_current_op(self):
        if not self.model_session_open or self.model_status in ("done", "scrapped"):
            return
        plan_op = self.plan_ops[self.model_idx]
        body = FinishBody(unit_id=self.unit.id)

        if not self.model_step_done[self.model_idx]:
            # (a): "no finish with open steps succeeding" -- must 409, no mutation.
            with pytest.raises(HTTPException) as exc_info:
                finish_operation(
                    plan_op_id=plan_op.id, body=body, station=self.station,
                    operator=self.operator, session=self.db,
                )
            assert exc_info.value.status_code == 409
            self.db.rollback()
            return

        if self.model_idx == self.N_OPS - 1:
            # last op -- satisfy the serial gate rather than special-casing it
            self.unit.serial_number = self.unit.serial_number or "SN-HYP-TEST"
            self.db.commit()

        result = finish_operation(
            plan_op_id=plan_op.id, body=body, station=self.station,
            operator=self.operator, session=self.db,
        )
        assert result["code"] == "finished", result

        self.model_session_open = False
        if self.model_idx + 1 < self.N_OPS:
            self.model_idx += 1
            self.model_status = "in_transit"
        else:
            self.model_status = "done"

    @rule()
    def rework_send_back_to_first_op(self):
        # O5: only meaningful mid-work, and only when there's an earlier op
        # to send back to -- exactly DD's "at_station -> rework(op K <=
        # current)".
        if self.model_idx == 0 or self.model_status != "at_station":
            return
        plan_op = self.plan_ops[self.model_idx]
        target_op = self.plan_ops[0]
        failures.record_failure(
            self.db, self.unit, plan_op, None, self.failure_code.id, "hypothesis rework",
            detected_by=self.operator, disposition="rework_to_op", rework_to_op=target_op,
            authorized_by=self.lead,
        )
        self.db.commit()

        for i in range(0, self.model_idx + 1):
            self.model_step_done[i] = False
        self.model_idx = 0
        self.model_status = "in_transit"
        self.model_session_open = False
        self.model_first_pass = False
        self.model_rework_count += 1

    # -- invariants -------------------------------------------------------------

    @invariant()
    def db_matches_model(self):
        self.db.expire_all()
        unit = self.db.get(Unit, self.unit.id)
        assert unit is not None

        # (a): no unit in an undefined status.
        assert unit.status in ("queued", "at_station", "in_transit", "done", "scrapped")
        assert unit.status == self.model_status

        # (e): first_pass never returns to True once flipped false, and
        # rework_count only ever increases -- checked as a hard equality
        # against the model (which itself only ever sets first_pass=False /
        # increments rework_count, never the reverse) across every rule
        # application, i.e. across however many rework loops this run drove.
        assert unit.first_pass == self.model_first_pass
        assert unit.rework_count == self.model_rework_count

        # current_plan_op_id is lazily resolved (None until the first scan
        # touches unit_next_op) -- only pin it down once it's actually set.
        if self.model_status != "done" and unit.current_plan_op_id is not None:
            assert unit.current_plan_op_id == self.plan_ops[self.model_idx].id

    @invariant()
    def at_most_one_open_session(self):
        # (b)/S10: this model has a single operator/station -- there must
        # never be more than one open WorkSession on the unit at a time (no
        # duplicate session from a re-scan).
        self.db.expire_all()
        open_sessions = self.db.execute(
            select(WorkSession).where(
                WorkSession.unit_id == self.unit.id, WorkSession.ended_at.is_(None)
            )
        ).scalars().all()
        assert len(open_sessions) <= 1


TestUnitWalk = UnitWalkMachine.TestCase
TestUnitWalk.settings = settings(max_examples=25, stateful_step_count=12, deadline=None)


# =============================================================================
# (b) -- idempotency, deterministic
# =============================================================================


def test_b_duplicate_scan_opens_no_second_session(db_session):
    station = _make_station(db_session)
    operator = _make_operator(db_session)
    _, plan_ops, unit = _seed_plan(db_session, n_ops=1)
    box = _bind_box(db_session, unit)

    r1 = statemachine.resolve_scan(db_session, box.qr_payload, station, operator)
    db_session.commit()
    assert r1.code == "accepted"

    r2 = statemachine.resolve_scan(db_session, box.qr_payload, station, operator)
    db_session.commit()
    assert r2.code == "already_active"

    sessions = db_session.execute(select(WorkSession)).scalars().all()
    assert len(sessions) == 1


def test_b_request_id_replay_returns_identical_response_and_no_side_effects(db_session):
    station = _make_station(db_session)
    operator = _make_operator(db_session)
    _, plan_ops, unit = _seed_plan(db_session, n_ops=1)
    box = _bind_box(db_session, unit)
    rid = str(uuid.uuid4())

    r1 = statemachine.resolve_scan(db_session, box.qr_payload, station, operator, request_id=rid)
    db_session.commit()
    r2 = statemachine.resolve_scan(db_session, box.qr_payload, station, operator, request_id=rid)
    db_session.commit()

    assert r1.to_dict() == r2.to_dict()
    assert len(db_session.execute(select(WorkSession)).scalars().all()) == 1


def test_b_double_finish_is_noop(db_session):
    station = _make_station(db_session)
    operator = _make_operator(db_session)
    _, plan_ops, unit = _seed_plan(db_session, n_ops=2)
    box = _bind_box(db_session, unit)
    statemachine.resolve_scan(db_session, box.qr_payload, station, operator)
    db_session.commit()
    _mark_step_done(db_session, unit, plan_ops[0])

    body = FinishBody(unit_id=unit.id)
    r1 = finish_operation(
        plan_op_id=plan_ops[0].id, body=body, station=station,
        operator=operator, session=db_session,
    )
    assert r1["code"] == "finished"

    events_before = _event_count(db_session)
    outbox_before = len(db_session.execute(select(JB2Outbox)).scalars().all())

    r2 = finish_operation(
        plan_op_id=plan_ops[0].id, body=body, station=station,
        operator=operator, session=db_session,
    )
    assert r2["code"] == "already_finished"
    assert _event_count(db_session) == events_before  # no new events on the no-op
    outbox_after = len(db_session.execute(select(JB2Outbox)).scalars().all())
    assert outbox_after == outbox_before  # no duplicate writeback


# =============================================================================
# (c) -- O1-O8 unreachable without the named role
# =============================================================================


@given(roles=st.lists(st.sampled_from(["operator", "lead", "quality"]), unique=True, max_size=3))
@settings(max_examples=20, deadline=None)
def test_c_second_badge_role_gate_is_role_exact(roles):
    """service.second_badge is the shared override gate behind O1 (skip),
    O2 (wrong-station accept), O3 (use_as_is authorizer), O8 (signoff) --
    property over every role combination: it must accept a role iff the
    operator actually holds it, nothing else."""
    engine = _make_engine()
    try:
        with Session(engine) as db_session:
            roles_list = list(roles) or ["operator"]
            operator = service.create_operator(
                db_session, display_name=f"Op-{uuid.uuid4()}", roles=roles_list,
            )
            db_session.commit()

            for role in ("lead", "quality"):
                # actor_operator_id=None -- isolates the role-gate property from
                # second_badge's separate "authorizer != acting operator" rule.
                if role in roles_list:
                    authorized = service.second_badge(
                        db_session, payload=operator.badge_qr, role=role,
                        actor_operator_id=None,
                    )
                    db_session.commit()
                    assert authorized.id == operator.id
                else:
                    with pytest.raises(service.SecondBadgeError):
                        service.second_badge(
                            db_session, payload=operator.badge_qr, role=role,
                            actor_operator_id=None,
                        )
                    db_session.commit()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "disposition,needs_target", [("scrap", False), ("rework_to_op", True)],
)
def test_c_scrap_and_rework_to_op_require_lead_authorizer(db_session, disposition, needs_target):
    """O4/O5: failures.record_failure enforces the lead requirement itself
    (not delegated to the caller) -- a non-lead authorizer must be rejected,
    a lead must succeed."""
    non_lead = _make_operator(db_session, name="NotLead", roles=["operator"])
    _, plan_ops, unit = _seed_plan(db_session, n_ops=2)
    failure_code = FailureCode(id=uuid.uuid4(), product_id=None, code="X", label="X", active=True)
    db_session.add(failure_code)
    db_session.commit()

    kwargs = {"rework_to_op": plan_ops[0]} if needs_target else {}

    with pytest.raises(failures.LeadRequiredError):
        failures.record_failure(
            db_session, unit, plan_ops[-1], None, failure_code.id, "n/a",
            detected_by=non_lead, disposition=disposition, authorized_by=non_lead, **kwargs,
        )

    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    failures.record_failure(  # must not raise
        db_session, unit, plan_ops[-1], None, failure_code.id, "n/a",
        detected_by=non_lead, disposition=disposition, authorized_by=lead, **kwargs,
    )


def test_c_lead_confirm_o6_requires_lead_role(db_session):
    station = _make_station(db_session)
    operator = _make_operator(db_session, name="Op")
    _, plan_ops, unit = _seed_plan(db_session, n_ops=1)
    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass",
    )
    statemachine.close_session(db_session, ws, reason="auto_closed")
    db_session.commit()

    with pytest.raises(sessions_domain.LeadRequiredError):
        sessions_domain.lead_confirm_session(db_session, ws, operator)  # not a lead

    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    sessions_domain.lead_confirm_session(db_session, ws, lead)  # must succeed
    assert ws.lead_confirmed is True


# =============================================================================
# (d) -- out-of-tolerance measurement blocks step completion until disposition
# =============================================================================


MEASUREMENT_SPEC = {"nominal": 1.0, "tol_plus": 0.01, "tol_minus": 0.01, "unit": "in"}


def test_d_out_of_tolerance_blocks_finish_until_disposition(db_session):
    station = _make_station(db_session)
    operator = _make_operator(db_session)
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    measurement_sub = lambda seq: [_sub(1, "measurement", spec=MEASUREMENT_SPEC)]  # noqa: E731
    _, plan_ops, unit = _seed_plan(db_session, n_ops=1, substeps_for_op=measurement_sub)
    box = _bind_box(db_session, unit)
    statemachine.resolve_scan(db_session, box.qr_payload, station, operator)
    db_session.commit()

    result = substeps.record_measurement(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=1,
        value="5.0",
    )
    db_session.commit()
    assert result["status"] == "failed"
    assert result["in_tolerance"] is False

    sub = db_session.execute(select(SubstepExecution)).scalars().one()
    assert sub.disposition is None

    # blocked: finish must 409 while the disposition is pending
    with pytest.raises(HTTPException) as exc_info:
        finish_operation(
            plan_op_id=plan_ops[0].id, body=FinishBody(unit_id=unit.id), station=station,
            operator=operator, session=db_session,
        )
    assert exc_info.value.status_code == 409
    db_session.rollback()

    # apply a disposition (O3: use_as_is, lead-or-quality authorized)
    failure_code = FailureCode(
        id=uuid.uuid4(), product_id=None, code="OOT", label="Out of tol", active=True,
    )
    db_session.add(failure_code)
    db_session.commit()
    substeps.apply_disposition(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=1,
        disposition="use_as_is", failure_code_id=failure_code.id, authorizer=lead,
    )
    db_session.commit()

    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)
    unit.serial_number = "SN-D-TEST"  # last op -- satisfy the serial gate, not this test's concern
    db_session.commit()

    result = finish_operation(
        plan_op_id=plan_ops[0].id, body=FinishBody(unit_id=unit.id), station=station,
        operator=operator, session=db_session,
    )
    assert result["code"] == "finished"
    assert result["unit_status"] == "done"


# =============================================================================
# (e) -- rework resets ops K..N; first_pass monotonic across multiple loops
# =============================================================================


def test_e_rework_loops_reset_range_and_first_pass_never_returns_true(db_session):
    from app.domain.models_floor import StepExecution

    operator = _make_operator(db_session)
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    _, plan_ops, unit = _seed_plan(db_session, n_ops=3)
    failure_code = FailureCode(id=uuid.uuid4(), product_id=None, code="X", label="X", active=True)
    db_session.add(failure_code)
    db_session.commit()

    def _mark_all_done_and_position_at_last():
        for op in plan_ops:
            db_session.add(
                StepExecution(
                    plan_operation_id=op.id, unit_id=unit.id, step_seq=1, status="done",
                    superseded=False,
                )
            )
        unit.current_plan_op_id = plan_ops[-1].id
        unit.status = "at_station"
        db_session.commit()

    def _current_step_exec(op):
        # the "live" (non-superseded) row -- each rework loop's fresh insert.
        return db_session.execute(
            select(StepExecution).where(
                StepExecution.plan_operation_id == op.id, StepExecution.unit_id == unit.id,
                StepExecution.superseded.is_(False),
            )
        ).scalars().one()

    assert unit.first_pass is True
    assert unit.rework_count == 0

    # -- loop 1: send back op3 -> op1 (resets the whole plan's only rows) --
    _mark_all_done_and_position_at_last()
    failures.record_failure(
        db_session, unit, plan_ops[2], None, failure_code.id, "loop1", detected_by=operator,
        disposition="rework_to_op", rework_to_op=plan_ops[0], authorized_by=lead,
    )
    db_session.commit()
    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)

    assert unit.first_pass is False
    assert unit.rework_count == 1
    assert unit.current_plan_op_id == plan_ops[0].id
    assert unit.status == "in_transit"
    for op in plan_ops:  # ops[0..2] all reset (range == whole plan) -- no live row left
        with pytest.raises(NoResultFound):
            _current_step_exec(op)

    # -- loop 2: redo (fresh rows), then send back op3 -> op2 --
    _mark_all_done_and_position_at_last()
    failures.record_failure(
        db_session, unit, plan_ops[2], None, failure_code.id, "loop2", detected_by=operator,
        disposition="rework_to_op", rework_to_op=plan_ops[1], authorized_by=lead,
    )
    db_session.commit()
    db_session.expire_all()
    unit = db_session.get(Unit, unit.id)

    assert unit.first_pass is False  # still false -- never returns to True
    assert unit.rework_count == 2  # monotonic increase across loops
    assert unit.current_plan_op_id == plan_ops[1].id
    assert _current_step_exec(plan_ops[0]).superseded is False  # before K -- untouched this loop
    with pytest.raises(NoResultFound):
        _current_step_exec(plan_ops[1])  # in range [K, current] -- superseded
    with pytest.raises(NoResultFound):
        _current_step_exec(plan_ops[2])


# =============================================================================
# (f) -- auto-closed session: no outbox row before O6, exactly one after
# =============================================================================


def test_f_auto_closed_session_outbox_withheld_until_lead_confirm(db_session):
    station = _make_station(db_session)
    operator = _make_operator_with_employee(db_session, employee_code="77")
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    _, plan_ops, unit = _seed_plan(db_session, n_ops=1)

    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass",
    )
    ws.lead_confirmed = False  # sqlite server_default quirk -- see _mark_step_done's note
    db_session.commit()

    now = datetime.now(timezone.utc)
    old_pause_start = now - timedelta(minutes=real_config.session_auto_close_min + 10)
    db_session.add(
        SessionPause(work_session_id=ws.id, reason_code="other", started_at=old_pause_start),
    )
    db_session.commit()

    closed = statemachine.auto_close_idle(
        db_session, now, idle_paused_minutes=real_config.session_auto_close_min,
    )
    db_session.commit()
    assert len(closed) == 1
    assert db_session.execute(select(JB2Outbox)).scalars().all() == []  # (f): nothing before O6

    sessions_domain.lead_confirm_session(db_session, ws, lead)
    db_session.commit()

    detail_rows = db_session.execute(
        select(JB2Outbox).where(JB2Outbox.kind == "time_ticket_detail")
    ).scalars().all()
    assert len(detail_rows) == 1  # (f): exactly one after O6

    # idempotent re-confirm -- still exactly one
    sessions_domain.lead_confirm_session(db_session, ws, lead)
    db_session.commit()
    detail_rows = db_session.execute(
        select(JB2Outbox).where(JB2Outbox.kind == "time_ticket_detail")
    ).scalars().all()
    assert len(detail_rows) == 1


# =============================================================================
# (g) -- every state transition emits exactly one events row
# =============================================================================


def test_g_open_session_emits_exactly_one_event(db_session):
    station = _make_station(db_session)
    operator = _make_operator(db_session)
    _, plan_ops, unit = _seed_plan(db_session, n_ops=1)

    before = _event_count(db_session)
    statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass",
    )
    db_session.commit()
    assert _event_count(db_session) - before == 1
    assert _event_count(db_session, "session.opened") == 1


def test_g_pause_resume_close_each_emit_exactly_one_event_and_zero_on_repeat(db_session):
    station = _make_station(db_session)
    operator = _make_operator(db_session)
    _, plan_ops, unit = _seed_plan(db_session, n_ops=1)
    ws = statemachine.open_session(
        db_session, unit=unit, operator=operator, station=station, plan_op=plan_ops[0],
        kind="first_pass",
    )
    db_session.commit()

    before = _event_count(db_session)
    statemachine.pause_session(db_session, ws, reason_code="break")
    db_session.commit()
    assert _event_count(db_session) - before == 1

    before = _event_count(db_session)
    statemachine.pause_session(db_session, ws, reason_code="break")  # idempotent -- already paused
    db_session.commit()
    assert _event_count(db_session) - before == 0

    before = _event_count(db_session)
    statemachine.resume_session(db_session, ws)
    db_session.commit()
    assert _event_count(db_session) - before == 1

    before = _event_count(db_session)
    statemachine.resume_session(db_session, ws)  # idempotent -- not paused
    db_session.commit()
    assert _event_count(db_session) - before == 0

    before = _event_count(db_session)
    statemachine.close_session(db_session, ws, reason="finished")
    db_session.commit()
    assert _event_count(db_session) - before == 1

    before = _event_count(db_session)
    statemachine.close_session(db_session, ws, reason="finished")  # double-close no-op
    db_session.commit()
    assert _event_count(db_session) - before == 0


def test_g_substep_complete_fail_skip_each_emit_exactly_one_event(db_session):
    station = _make_station(db_session)
    operator = _make_operator(db_session)
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    _, plan_ops, unit = _seed_plan(
        db_session, n_ops=1,
        substeps_for_op=lambda seq: [_sub(1, "action"), _sub(2, "action"), _sub(3, "action")],
    )
    box = _bind_box(db_session, unit)
    statemachine.resolve_scan(db_session, box.qr_payload, station, operator)
    db_session.commit()

    before = _event_count(db_session)
    substeps.complete_substep(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=1,
    )
    db_session.commit()
    assert _event_count(db_session) - before == 1

    before = _event_count(db_session)
    substeps.fail_substep(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=2,
    )
    db_session.commit()
    assert _event_count(db_session) - before == 1

    before = _event_count(db_session)
    substeps.skip_substep(
        db_session, station=station, operator=operator, unit_id=unit.id, step_seq=1, substep_seq=3,
        skip_reason="n/a", lead=lead,
    )
    db_session.commit()
    assert _event_count(db_session) - before == 1


def test_g_o2_override_accept_is_the_documented_multi_event_exception(db_session):
    """Guards the deliberate exception to (g) found in the codebase: a scan
    accept opens a WorkSession (its own `session.opened` fact) AND records
    the scan itself (`scan.accepted`); an O2 wrong-station override adds a
    third fact, `auth.override` (who authorized bypassing the sequence).
    Three rows, one call, by design -- each names a distinct audited fact,
    not a duplicate-emit bug. If this ever regresses to a different count,
    something about the accept/override bookkeeping changed."""
    station = _make_station(db_session, work_center_code="ASSY1")
    operator = _make_operator(db_session, name="Op")
    lead = _make_operator(db_session, name="Lead", roles=["operator", "lead"])
    _, plan_ops, unit = _seed_plan(db_session, work_centers=("CNC1", "ASSY1"))
    box = _bind_box(db_session, unit)

    before = _event_count(db_session)
    result = statemachine.resolve_scan(
        db_session, box.qr_payload, station, operator, override_by=lead,
    )
    db_session.commit()

    assert result.code == "accepted"
    assert _event_count(db_session) - before == 3
    assert _event_count(db_session, "scan.accepted") == 1
    assert _event_count(db_session, "session.opened") == 1
    assert _event_count(db_session, "auth.override") == 1
