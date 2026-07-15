"""Integration tests for P4-02: app/domain/pipeline.py + app/api/dashboard.py.

Same standalone-FastAPI-against-a-fresh-sqlite-engine pattern as
tests/integration/test_finish_and_writeback.py -- domain-level seeding that
bypasses app.sync/app.domain.workorders, one unit per CR-004 dashboard
state, then asserted against the JSON board.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import app.api.dashboard as dashboard_module
from app.api.dashboard import get_session_factory
from app.api.dashboard import router as dashboard_router
from app.auth import service
from app.db import get_session
from app.domain import events
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_floor import Event, Failure, Transit, WorkSession
from app.domain.models_jb2 import Base, JB2OrderLineItem
from app.domain.models_library import FailureCode, Product
from app.main import app as _main_app  # noqa: F401 -- registers the full model graph

# Real-clock reference: the dashboard endpoint derives its own now via
# datetime.now(), so relative session offsets below (e.g. "5h ago" -> stalled)
# must be anchored to the same real clock. Absolute due-dates stay fixed; the
# overdue-days assertion is computed against NOW's date so it never drifts.
NOW = datetime.now(timezone.utc)
_OVERDUE_DUE = date(2026, 7, 1)


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    original_type = Event.__table__.c.id.type
    Event.__table__.c.id.type = Integer()
    try:
        Base.metadata.create_all(eng)
    finally:
        Event.__table__.c.id.type = original_type
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine, db_session, monkeypatch):
    app = FastAPI()
    app.include_router(dashboard_router)

    session_maker = sessionmaker(bind=engine)

    def _override_session():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    def _override_factory():
        return session_maker

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_session_factory] = _override_factory
    # fast poll loop -- these tests don't want to wait a full second per tick.
    monkeypatch.setattr(dashboard_module, "POLL_INTERVAL_S", 0.05)
    with TestClient(app) as c:
        yield c


def _mirror_common() -> dict:
    return {"content_hash": "h", "jb2_last_modified": None, "synced_at": datetime.now(timezone.utc)}


def _seed_wo(
    db_session: Session, *, due_date: date | None = None, wo_status: str = "in_progress",
    op_work_centers: tuple[str, ...] = ("CNC1", "ASSY1"), job_number: str = "J-1",
) -> tuple[WorkOrder, list[PlanOperation]]:
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"li-{uuid.uuid4()}", part_number="APOLLO-9-BLK",
        description="Apollo 9mm", qty=1, payload={"jobNumber": job_number}, **_mirror_common(),
    )
    db_session.add(line_item)
    db_session.flush()

    product = db_session.query(Product).filter_by(name="Apollo").one_or_none()
    if product is None:  # one shared product across all seeded work orders (name is unique)
        product = Product(id=uuid.uuid4(), name="Apollo", variant_schema={}, active=True)
        db_session.add(product)
        db_session.flush()

    work_order = WorkOrder(
        id=uuid.uuid4(), jb2_line_item_id=line_item.id, product_id=product.id,
        qty=1, status=wo_status, due_date=due_date,
    )
    db_session.add(work_order)
    db_session.flush()

    plan_ops = []
    for seq, wc in enumerate(op_work_centers, start=1):
        plan_op = PlanOperation(
            id=uuid.uuid4(), work_order_id=work_order.id, seq=seq,
            operation_code=f"OP{seq * 10}", title=f"Op {seq} ({wc})",
            frozen_content={
                "steps": [{"seq": 1, "title": "Step 1"}, {"seq": 2, "title": "Step 2"}]
            },
            status="pending",
        )
        db_session.add(plan_op)
        plan_ops.append(plan_op)
    db_session.flush()
    return work_order, plan_ops


def _seed_unit(
    db_session, work_order, *, status: str, first_pass: bool = True, rework_count: int = 0,
    current_plan_op=None, serial: str | None = None,
) -> Unit:
    unit = Unit(
        id=uuid.uuid4(), work_order_id=work_order.id, unit_no=1, status=status,
        first_pass=first_pass, rework_count=rework_count,
        current_plan_op_id=current_plan_op.id if current_plan_op else None,
        serial_number=serial,
    )
    db_session.add(unit)
    db_session.flush()
    return unit


def _station_and_operator(db_session, *, wc="CNC1"):
    station, _ = service.create_station(db_session, name=f"Station-{wc}", work_center_code=wc)
    operator = service.create_operator(db_session, display_name="Op A", roles=["operator"])
    lead = service.create_operator(db_session, display_name="Lead L", roles=["operator", "lead"])
    db_session.commit()
    return station, operator, lead


def _open_session(db_session, unit, plan_op, operator, station, *, started_at, kind="first_pass"):
    ws = WorkSession(
        unit_id=unit.id, plan_operation_id=plan_op.id, operator_id=operator.id,
        station_id=station.id, started_at=started_at, kind=kind,
    )
    db_session.add(ws)
    db_session.flush()
    return ws


def _closed_session(
    db_session, unit, plan_op, operator, station, *, started_at, ended_at, reason="finished",
):
    ws = WorkSession(
        unit_id=unit.id, plan_operation_id=plan_op.id, operator_id=operator.id,
        station_id=station.id, started_at=started_at, ended_at=ended_at, kind="first_pass",
        close_reason=reason,
    )
    db_session.add(ws)
    db_session.flush()
    return ws


@pytest.fixture
def board(db_session):
    """Seeds one unit per CR-004 dashboard state; returns a dict of
    state name -> unit id, plus the raw units for assertions."""
    station, operator, lead = _station_and_operator(db_session)
    units: dict[str, Unit] = {}

    # 1. first_pass: active work session, recent, not overdue.
    wo1, ops1 = _seed_wo(db_session, due_date=date(2026, 8, 15), job_number="J-FP")
    u1 = _seed_unit(db_session, wo1, status="at_station", current_plan_op=ops1[0])
    _open_session(
        db_session, u1, ops1[0], operator, station, started_at=NOW - timedelta(minutes=20)
    )
    units["first_pass"] = u1

    # 2. rework x2: not first pass, active session, on track otherwise.
    wo2, ops2 = _seed_wo(db_session, due_date=date(2026, 8, 15), job_number="J-RW")
    u2 = _seed_unit(
        db_session, wo2, status="at_station", first_pass=False, rework_count=2,
        current_plan_op=ops2[1],
    )
    _open_session(
        db_session, u2, ops2[1], operator, station, started_at=NOW - timedelta(minutes=15)
    )
    failure_code = FailureCode(
        id=uuid.uuid4(), product_id=None, code="F-114", label="rail galling", active=True
    )
    db_session.add(failure_code)
    db_session.flush()
    db_session.add(Failure(
        unit_id=u2.id, plan_operation_id=ops2[1].id, failure_code_id=failure_code.id,
        detected_by=operator.id, disposition="rework_in_place",
        detected_at=NOW - timedelta(hours=1),
    ))
    units["rework"] = u2

    # 3. stalled: at_station, no open session, closed one ended 3h ago (> 2h queue-dwell default).
    wo3, ops3 = _seed_wo(db_session, due_date=date(2026, 8, 15), job_number="J-ST")
    u3 = _seed_unit(db_session, wo3, status="at_station", current_plan_op=ops3[0])
    _closed_session(
        db_session, u3, ops3[0], operator, station,
        started_at=NOW - timedelta(hours=5), ended_at=NOW - timedelta(hours=3),
    )
    units["stalled"] = u3

    # 4. overdue: due date in the past, otherwise a normal active session.
    wo4, ops4 = _seed_wo(db_session, due_date=_OVERDUE_DUE, job_number="J-OD")
    u4 = _seed_unit(db_session, wo4, status="at_station", current_plan_op=ops4[0])
    _open_session(
        db_session, u4, ops4[0], operator, station, started_at=NOW - timedelta(minutes=10)
    )
    units["overdue"] = u4

    # 5. queued / grey: brand new, never touched.
    wo5, ops5 = _seed_wo(db_session, due_date=date(2026, 9, 10), job_number="J-Q")
    u5 = _seed_unit(db_session, wo5, status="queued", current_plan_op=ops5[0])
    units["queued"] = u5

    # 6. in_transit: open transit hop departed 5 min ago (well under dwell threshold).
    wo6, ops6 = _seed_wo(db_session, due_date=date(2026, 8, 1), job_number="J-IT")
    u6 = _seed_unit(db_session, wo6, status="in_transit", current_plan_op=ops6[1])
    db_session.add(Transit(
        unit_id=u6.id, from_station_id=station.id, departed_at=NOW - timedelta(minutes=5),
    ))
    units["in_transit"] = u6

    # 7. blocked_no_instructions: work order flagged blocked.
    wo7, ops7 = _seed_wo(
        db_session, due_date=date(2026, 9, 1),
        wo_status="blocked_no_instructions", job_number="J-BL",
    )
    u7 = _seed_unit(db_session, wo7, status="queued", current_plan_op=ops7[0])
    units["blocked_no_instructions"] = u7

    db_session.commit()
    return units


def test_pipeline_json_assigns_correct_state_and_color_per_card(client, board):
    resp = client.get("/api/v1/dashboard/pipeline")
    assert resp.status_code == 200
    body = resp.json()
    cards = body["groups"][0]["cards"]
    assert len(cards) == 7

    by_unit = {c["unit_id"]: c for c in cards}
    expected_state = {
        "first_pass": "first_pass",
        "rework": "rework",
        "stalled": "stalled",
        "overdue": "overdue",
        "queued": "queued",
        "in_transit": "in_transit",
        "blocked_no_instructions": "blocked_no_instructions",
    }
    from app.domain.pipeline import STATE_COLOR

    for name, unit in board.items():
        card = by_unit[str(unit.id)]
        assert card["state"] == expected_state[name], name
        assert card["color"] == STATE_COLOR[expected_state[name]], name

    assert by_unit[str(board["rework"].id)]["badge_label"] == "Rework ×2"
    assert by_unit[str(board["queued"].id)]["badge_label"] == "Awaiting start"
    expected_overdue = (NOW.date() - _OVERDUE_DUE).days
    assert by_unit[str(board["overdue"].id)]["overdue_days"] == expected_overdue


def test_kpis_match_db(client, board):
    resp = client.get("/api/v1/dashboard/pipeline")
    kpis = resp.json()["kpis"]
    assert kpis["wip"] == 7
    # first_pass True on 6/7 units (only "rework" is first_pass=False)
    assert kpis["first_pass_pct"] == round(100 * 6 / 7)
    assert kpis["in_rework"] == 1
    assert kpis["stalled"] == 1
    assert kpis["on_time_pct"] == round(100 * 6 / 7)  # 1 overdue out of 7


def test_grouping_toggle_regroups_by_station(client, board):
    resp = client.get("/api/v1/dashboard/pipeline?group_by=station")
    body = resp.json()
    assert body["group_by"] == "station"
    total = sum(len(g["cards"]) for g in body["groups"])
    assert total == 7
    assert len(body["groups"]) > 1  # units sit at different stations/ops

    ungrouped = client.get("/api/v1/dashboard/pipeline").json()
    assert ungrouped["group_by"] is None
    assert len(ungrouped["groups"]) == 1


class _NeverDisconnects:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_dashboard_stream_emits_refresh_after_new_event(
    engine, db_session, board, monkeypatch
):
    """Drives the route function directly (bypassing TestClient's own
    streaming machinery, which has no read timeout to bound a hang if this
    ever regresses) with an explicit `asyncio.wait_for` bound instead."""
    monkeypatch.setattr(dashboard_module, "POLL_INTERVAL_S", 0.02)
    session_maker = sessionmaker(bind=engine)

    resp = await dashboard_module.dashboard_stream(
        _NeverDisconnects(), session_factory=lambda: session_maker()
    )
    unit_id = board["first_pass"].id

    async def _read_until_refresh() -> bool:
        async for chunk in resp.body_iterator:
            if "event: refresh" in chunk:
                return True
        return False

    async def _emit_after_baseline() -> None:
        # The stream captures its last-seen event id on the first poll; emit
        # only after that, or the new row is already in the baseline and never
        # reads as "new". One poll interval of slack is enough.
        await asyncio.sleep(dashboard_module.POLL_INTERVAL_S * 3)
        with session_maker() as s:
            unit = s.get(Unit, unit_id)
            events.emit(s, "unit.moved", entity=unit, after={"x": 1})
            s.commit()

    reader = asyncio.ensure_future(_read_until_refresh())
    await _emit_after_baseline()
    assert await asyncio.wait_for(reader, timeout=3)


def test_tv_mode_hides_admin_nav(client, board, monkeypatch):
    # dashboard_page renders via Jinja2Templates pointed at REPO_ROOT/templates,
    # independent of this test app's router set -- exercise it directly.

    plain = client.get("/dashboard")
    assert plain.status_code == 200
    assert "tv-link" in plain.text or "Admin" in plain.text  # normal mode shows nav

    tv = client.get("/dashboard?tv=1")
    assert tv.status_code == 200
    assert "dash-nav" not in tv.text
    assert "Admin" not in tv.text
