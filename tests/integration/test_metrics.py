"""Integration tests for P4-04 (metrics SQL views, DD §13.2/§9.11).

Seeds a small, fully-known dataset directly via the ORM (not through the
floor/domain flows -- this test is about the SQL views' arithmetic, not
state-machine behavior, which tests/integration/test_fail_flow.py and
friends already cover) and hand-checks every view's aggregated output:

  - FPY = 1/2 (unit A first-pass, unit B reworked-then-done, both `done`)
  - operation-level FPY (same 1/2, one rework hit at the one operation)
  - scrap Pareto: exactly 1 scrap event, known cause + material value
  - throughput: 2 units done the same day
  - actual-vs-estimate: known delta (24 actual min vs. 20 estimated)
  - queue time: known average (240s across the two sessions)
  - rework %: known share (9 of 24 total minutes = 37.5%)
  - WIP age: one non-done/scrapped unit, ~30h old

Runs entirely on sqlite (this repo's integration tests build schema via
`Base.metadata.create_all`, never by running Alembic in-process) using the
exact view SQL app/domain/metrics.py also hands to migrations/versions/
0008_metrics_views.py for Postgres. Every view here has a working sqlite
variant (see that module's dialect-branch helpers) -- **none are
Postgres-only, so nothing is skipped in this run.** The live-Postgres
migration apply (`alembic upgrade head` against the pg16 instance) is the
separate verification the task brief asks for; reported in the task's final
summary, not repeated here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api import metrics as metrics_api
from app.api.metrics import router as metrics_router
from app.db import get_session
from app.domain import metrics as metrics_domain
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import (
    Failure,
    Operator,
    ScrapEvent,
    Station,
    StepExecution,
    Transit,
    WorkSession,
)
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting
from app.domain.models_library import FailureCode, Product


def _mirror_common(now: datetime) -> dict:
    return {"content_hash": "h", "jb2_last_modified": None, "synced_at": now}


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        metrics_domain.create_views(conn, is_postgres=False)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(metrics_router)

    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


def _seed(db_session: Session) -> dict:
    """2 primary units (A: clean first-pass, B: one rework_in_place then
    done) + a 3rd scrapped unit (C) whose scrap mints a replacement (D,
    still queued) -- matches the brief's "2 units, 1 rework, 1 scrap" as
    countable *events*, not a literal 2-row `units` table (a scrap always
    mints a replacement per DD §6.5, so the table necessarily has 4 rows)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC -- sqlite-native storage

    product = Product(id=uuid.uuid4(), name="Apollo", variant_schema={}, active=True)
    db_session.add(product)
    db_session.flush()

    created_at = now - timedelta(hours=30)  # -> v_wip_age's known ~30h bucket
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=4, payload={}, **_mirror_common(now),
    )
    db_session.add(line_item)
    db_session.flush()
    work_order = WorkOrder(
        id=uuid.uuid4(), jb2_line_item_id=line_item.id, product_id=product.id, qty=4,
        status="in_progress", created_at=created_at, updated_at=created_at,
    )
    db_session.add(work_order)
    db_session.flush()

    routing = JB2OrderRouting(
        id=uuid.uuid4(), jb2_id=f"routing-{work_order.id}", jb2_line_item_id=line_item.id,
        seq=1, operation_code="OP10", description="Assembly", work_center_code="ASSY1",
        payload={}, **_mirror_common(now),
    )
    db_session.add(routing)
    db_session.flush()
    plan_op = PlanOperation(
        id=uuid.uuid4(), work_order_id=work_order.id, seq=1, jb2_routing_id=routing.id,
        operation_code="OP10", title="Op 1 (ASSY1)", frozen_content={"steps": []},
        status="done", est_minutes=10,
    )
    db_session.add(plan_op)
    db_session.flush()

    station = Station(
        id=uuid.uuid4(), name="Assy bench", work_center_code="ASSY1", kiosk_token=str(uuid.uuid4()),
    )
    operator = Operator(
        id=uuid.uuid4(), display_name="Alice", badge_qr=f"OP:{uuid.uuid4()}",
        roles=["operator"], active=True,
    )
    lead = Operator(
        id=uuid.uuid4(), display_name="Lead", badge_qr=f"OP:{uuid.uuid4()}",
        roles=["operator", "lead"], active=True,
    )
    db_session.add_all([station, operator, lead])
    db_session.flush()

    unit_a = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=1, status="done",
        first_pass=True, rework_count=0, completed_at=now,
    )
    unit_b = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=2, status="done",
        first_pass=False, rework_count=1, completed_at=now,
    )
    unit_c = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=3, status="scrapped",
        first_pass=True, rework_count=0,
    )
    unit_d = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=4, status="queued",
        first_pass=True, rework_count=0,
    )
    db_session.add_all([unit_a, unit_b, unit_c, unit_d])
    db_session.flush()
    unit_d.remake_of_unit_id = unit_c.id
    db_session.flush()

    # step_executions -- v_fpy_operation joins against these to know which
    # units actually reached this plan_operation.
    db_session.add_all([
        StepExecution(
            plan_operation_id=plan_op.id, unit_id=unit_a.id, step_seq=1, status="done",
            superseded=False,
        ),
        StepExecution(
            plan_operation_id=plan_op.id, unit_id=unit_b.id, step_seq=1, status="done",
            superseded=False,
        ),
    ])

    # -- rework: a Failure row against unit B at plan_op (rework_in_place)
    failure_code_rework = FailureCode(
        id=uuid.uuid4(), product_id=None, code="MISALIGN", label="Misaligned", active=True,
    )
    db_session.add(failure_code_rework)
    db_session.flush()
    db_session.add(Failure(
        id=uuid.uuid4(), unit_id=unit_b.id, plan_operation_id=plan_op.id, substep_execution_id=None,
        failure_code_id=failure_code_rework.id, narrative="misaligned", detected_by=operator.id,
        detected_at=now, disposition="rework_in_place", authorized_by=None,
    ))

    # -- scrap: a Failure + ScrapEvent against unit C
    failure_code_scrap = FailureCode(
        id=uuid.uuid4(), product_id=None, code="CRACK", label="Cracked frame", active=True,
    )
    db_session.add(failure_code_scrap)
    db_session.flush()
    scrap_failure = Failure(
        id=uuid.uuid4(), unit_id=unit_c.id, plan_operation_id=plan_op.id, substep_execution_id=None,
        failure_code_id=failure_code_scrap.id, narrative="cracked", detected_by=operator.id,
        detected_at=now, disposition="scrap", authorized_by=lead.id,
    )
    db_session.add(scrap_failure)
    db_session.flush()
    db_session.add(ScrapEvent(
        id=uuid.uuid4(), unit_id=unit_c.id, failure_id=scrap_failure.id, cause_code="CRACK",
        narrative="cracked", material_value_est=Decimal("150.00"), authorized_by=lead.id,
        created_at=now, replacement_unit_id=unit_d.id,
    ))

    # -- work sessions: unit A 15 min (first_pass), unit B 9 min (rework) --
    # both closed, no pauses (net-of-pauses math is exercised by the
    # session_pauses LEFT JOIN in v_work_session_minutes returning 0/NULL).
    db_session.add(WorkSession(
        id=uuid.uuid4(), unit_id=unit_a.id, plan_operation_id=plan_op.id, operator_id=operator.id,
        station_id=station.id, started_at=now - timedelta(minutes=15), ended_at=now,
        kind="first_pass", close_reason="finished",
    ))
    db_session.add(WorkSession(
        id=uuid.uuid4(), unit_id=unit_b.id, plan_operation_id=plan_op.id, operator_id=operator.id,
        station_id=station.id, started_at=now - timedelta(minutes=9), ended_at=now,
        kind="rework", close_reason="finished",
    ))

    # -- transits: queue time before each session (5 min for A, 3 min for B)
    db_session.add(Transit(
        id=uuid.uuid4(), unit_id=unit_a.id, from_station_id=None, to_station_id=station.id,
        departed_at=now - timedelta(minutes=25), arrived_at=now - timedelta(minutes=20),
        seconds=300,
    ))
    db_session.add(Transit(
        id=uuid.uuid4(), unit_id=unit_b.id, from_station_id=None, to_station_id=station.id,
        departed_at=now - timedelta(minutes=15), arrived_at=now - timedelta(minutes=12),
        seconds=180,
    ))

    db_session.commit()
    return {"now": now, "far_past_cutoff": now - timedelta(days=3650)}


def test_fpy_unit_is_one_half(db_session):
    ctx = _seed(db_session)
    rows = metrics_api.fpy_by_product(db_session, ctx["far_past_cutoff"])
    assert len(rows) == 1
    row = rows[0]
    assert row["product_name"] == "Apollo"
    assert row["units_done"] == 2
    assert row["units_first_pass"] == 1
    assert row["fpy_pct"] == pytest.approx(50.0)


def test_fpy_operation_is_one_half(db_session):
    _seed(db_session)
    rows = metrics_api.fpy_by_operation(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row["operation_code"] == "OP10"
    assert row["units_reached"] == 2
    assert row["units_clean"] == 1
    assert row["fpy_pct"] == pytest.approx(50.0)


def test_scrap_pareto_one_event(db_session):
    ctx = _seed(db_session)
    rows = metrics_api.scrap_pareto(db_session, ctx["far_past_cutoff"])
    assert len(rows) == 1
    row = rows[0]
    assert row["cause_code"] == "CRACK"
    assert row["cause_label"] == "Cracked frame"
    assert row["scrap_count"] == 1
    assert row["material_value_total"] == pytest.approx(150.0)


def test_throughput_two_units_same_day(db_session):
    ctx = _seed(db_session)
    rows = metrics_api.throughput_daily(db_session, ctx["far_past_cutoff"])
    assert len(rows) == 1
    assert rows[0]["product_name"] == "Apollo"
    assert rows[0]["units_done"] == 2


def test_actual_vs_estimate_known_delta(db_session):
    _seed(db_session)
    rows = metrics_api.actual_vs_estimate(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row["operation_code"] == "OP10"
    assert row["units_worked"] == 2
    assert row["actual_minutes_total"] == pytest.approx(24.0)  # 15 + 9
    assert row["est_minutes_total"] == pytest.approx(20.0)  # 10 * 2
    assert row["delta_minutes"] == pytest.approx(4.0)


def test_queue_time_known_average(db_session):
    ctx = _seed(db_session)
    rows = metrics_api.queue_time_by_station(db_session, ctx["far_past_cutoff"])
    assert len(rows) == 1
    row = rows[0]
    assert row["station_name"] == "Assy bench"
    assert row["sessions"] == 2
    assert row["avg_queue_seconds"] == pytest.approx(240.0)  # (300 + 180) / 2


def test_rework_hours_pct_known_share(db_session):
    _seed(db_session)
    result = metrics_api.rework_hours_pct(db_session)
    assert result["rework_minutes"] == pytest.approx(9.0)
    assert result["total_minutes"] == pytest.approx(24.0)
    assert result["rework_pct"] == pytest.approx(37.5)


def test_wip_age_one_unit_around_30_hours(db_session):
    _seed(db_session)
    buckets = metrics_api.wip_age_histogram(db_session)
    counts = {b["bucket"]: b["count"] for b in buckets}
    assert sum(counts.values()) == 1  # only unit D is neither done nor scrapped
    assert counts["24-48h"] == 1
    assert counts["<24h"] == 0
    assert counts["48-96h"] == 0
    assert counts["96h+"] == 0


def test_dashboard_metrics_page_renders(db_session, client):
    """Smoke test: the full GET /dashboard/metrics page renders with real
    numbers baked in (bar widths, table rows) -- not just the underlying
    query functions in isolation."""
    _seed(db_session)
    resp = client.get("/dashboard/metrics?days=3650")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "Apollo" in html
    assert "Cracked frame" in html
    assert "37.5" in html  # rework %
    assert "24-48h" in html
