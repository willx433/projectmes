"""Gate 3 end-to-end walk harness (DD §19 Phase 3 accept: "full happy-path
unit travels 3 stations end-to-end; JB2 shows correct time tickets and
quantities; all §9.1-9.4 data queryable").

This is gate EVIDENCE, not a unit test: it drives one unit through 3
stations (CNC -> Fit -> QC) over the REAL HTTP stack (FastAPI TestClient
against the actual routers in app.main, real cookie/token auth, real
statemachine/substeps/finish domain code) against REAL PostgreSQL, then
drains the resulting jb2_outbox rows into an in-process fake-JB2 server and
asserts the CR-010 write-set (time-tickets + time-ticket-details only, ZERO
/order-routings PATCH).

Reuses the exact seeding/harness patterns already proven in:
  - tests/integration/test_finish_and_writeback.py (TestClient app build,
    get_session override, fake-JB2 drain-and-diff)
  - tests/integration/test_station_flow.py (substep HTTP walk)
  - tests/integration/test_boxes.py (kit-up via /admin/kitup)
  - tools/seed_demo.py (building a real product/instruction-set/work-order
    tree through app.domain.library / app.domain.workorders, no raw INSERTs
    for anything the domain layer already has a builder for)

Usage:
    .venv/bin/python tools/gate3_walk.py [--db URL] [--tag NAME]

ponytail: every identifier below is namespaced with a fresh per-run `tag`
(timestamp + short random suffix) rather than a fixed value + cross-table
cascade-delete cleanup -- Product.name/ProductPartMap.jb2_part_number/
JB2Employee.jb2_id etc. are unique-constrained and several FKs in this
schema have no ON DELETE CASCADE (by design, see models_floor.py's
docstring), so "clean up the previous run" would need a hand-maintained
child-table deletion order for little benefit here. A fresh tag makes the
run idempotent by construction. Passing --tag pins your own value (e.g. for
a stable demo) -- re-running with the SAME --tag against the SAME database
will collide on those unique constraints; that's an accepted ceiling for a
gate-evidence script, not a service.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # repo isn't pip-installed (tools/seed_demo.py's fix)

# MUST be set before the first `from app.config import config` anywhere below
# (app/config.py loads its single module-level `config` at import time) --
# there is no MES_SECRET_KEY in .env, and app/auth/deps.py raises a 500
# rather than sign cookies with an empty secret.
os.environ.setdefault("MES_SECRET_KEY", "gate3-walk-harness-secret-not-for-prod")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.exc import OperationalError, SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.auth import service  # noqa: E402
from app.db import get_session  # noqa: E402
from app.domain import events, library, workorders  # noqa: E402
from app.domain.models_execution import PlanOperation, Unit, WorkOrder  # noqa: E402
from app.domain.models_floor import (  # noqa: E402
    BuildBox,
    Event,
    Measurement,
    Scan,
    ScrapEvent,
    StepExecution,
    SubstepExecution,
    Transit,
    WorkSession,
)
from app.domain.models_jb2 import (  # noqa: E402
    Base,
    JB2Employee,
    JB2OrderLineItem,
    JB2OrderRouting,
    JB2WorkCenter,
)
from app.domain.models_library import Product, ProductPartMap  # noqa: E402
from app.jb2.client import Jb2Client  # noqa: E402
from app.outbox import drainer  # noqa: E402
from tests.fake_jb2 import create_fake_jb2, seed_state  # noqa: E402

DEFAULT_PG_URL = "postgresql+psycopg://mes@127.0.0.1:55432/mes"

# The 3 stations the unit travels, in order.
STATIONS = [
    {"wc": "CNC1", "name": "Gate3 CNC", "op_code": "OP10", "title": "Gate3 CNC Slide"},
    {"wc": "FIT1", "name": "Gate3 Fit", "op_code": "OP20", "title": "Gate3 Barrel Fit"},
    {"wc": "QC1", "name": "Gate3 QC", "op_code": "OP30", "title": "Gate3 Final QC"},
]

MEASUREMENT_SPEC = {"nominal": 0.5, "tol_plus": 0.01, "tol_minus": 0.01, "unit": "in"}

# op index -> substeps (each a dict consumed by library.add_substep, same
# shape as tools/seed_demo.py's _build_instruction_set helper). Covers: one
# measurement-with-tolerances substep (op 0) + one signoff substep (op 2),
# per the task brief.
SUBSTEPS_BY_OP = [
    [
        {"type": "action", "title": "Load fixture"},
        {"type": "measurement", "title": "Verify chamber depth", "measurement_spec": MEASUREMENT_SPEC},
    ],
    [
        {"type": "action", "title": "Press-fit barrel to slide"},
        {"type": "action", "title": "Verify fit by hand"},
    ],
    [
        {"type": "inspection", "title": "Visual/function inspection"},
        {"type": "signoff", "title": "QC signoff", "signoff_role": "lead"},
    ],
]


class GateWalkError(RuntimeError):
    """A step of the walk produced an unexpected result -- surfaced as a
    Gate 3 defect (not swallowed), per the task brief."""


def _fresh_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S") + "-" + uuid.uuid4().hex[:6]


# -- DB connect / schema ------------------------------------------------------


def resolve_engine(cli_db: str | None) -> tuple[object, bool, list[str]]:
    """Returns (engine, using_postgres, notices). Tries `cli_db` or
    DATABASE_URL or the known-live pg instance first; falls back to a fresh
    file-based sqlite (never in-memory -- task requires file-based) if
    unreachable."""
    notices: list[str] = []
    url = cli_db or os.environ.get("DATABASE_URL") or DEFAULT_PG_URL
    if url.startswith("postgresql://"):  # psycopg3 scheme coercion (migrations/env.py's fix)
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    if url.startswith("postgresql"):
        try:
            engine = create_engine(url, pool_pre_ping=True)
            with engine.connect():
                pass
            notices.append(f"Connected to PostgreSQL: {url}")
            return engine, True, notices
        except (OperationalError, SQLAlchemyError, OSError) as exc:
            notices.append(f"!! Could not connect to {url}: {exc}")

    fallback_path = Path(tempfile.gettempdir()) / f"gate3_walk_fallback_{uuid.uuid4().hex[:8]}.sqlite3"
    notices.append(
        f"!! FALLING BACK to a fresh file-based sqlite (Postgres unreachable): {fallback_path}"
    )
    engine = create_engine(f"sqlite:///{fallback_path}", connect_args={"check_same_thread": False})
    return engine, False, notices


def ensure_schema(engine, using_postgres: bool) -> str:
    if using_postgres:
        return "Postgres: assuming schema already at head via alembic (no create_all)."
    # Same sqlite bigint-autoincrement workaround every integration test in
    # this repo uses for Event.id (Postgres bigserial has no sqlite analog).
    original_type = Event.__table__.c.id.type
    from sqlalchemy import Integer as _Integer

    Event.__table__.c.id.type = _Integer()
    try:
        Base.metadata.create_all(engine)
    finally:
        Event.__table__.c.id.type = original_type
    return "sqlite fallback: Base.metadata.create_all() ran to build a fresh schema."


# -- seeding (real domain layer only, no raw INSERTs) -------------------------


def _mirror_common(now: datetime) -> dict:
    return {"content_hash": "gate3", "jb2_last_modified": None, "synced_at": now}


def _build_instruction_set(session: Session, product: Product, *, title: str, op_code: str,
                            substeps: list[dict]) -> None:
    iset = library.create_set(
        session, product_id=product.id, title=title,
        operation_match={"op_codes": [op_code], "work_centers": []}, created_by="gate3-walk",
    )
    session.flush()
    step = library.add_step(session, iset.id, f"{title} step", seq=1, who="gate3-walk")
    for seq, sub in enumerate(substeps, start=1):
        sub = dict(sub)
        library.add_substep(
            session, step.id, sub.pop("type"), sub.pop("title"), seq=seq, who="gate3-walk", **sub
        )
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "gate3-walk-approver")


def seed(session: Session, tag: str) -> dict:
    """Builds the full Apollo-shaped product/instruction-set/work-order tree
    through app.domain.library / app.domain.workorders (same real-domain-
    layer approach as tools/seed_demo.py), plus the floor identities
    (stations/operators/failure code) via app.auth.service /
    app.domain.library, and the kit-up prerequisites. Returns everything the
    walk needs, keyed by name."""
    now = datetime.now(timezone.utc)

    product = Product(
        id=uuid.uuid4(), name=f"Apollo Gate3 {tag}",
        description="Gate 3 walk harness product.", variant_schema={}, active=True,
    )
    session.add(product)
    session.flush()

    part_number = f"GATE3-{tag}-BLK"
    session.add(
        ProductPartMap(id=uuid.uuid4(), product_id=product.id, jb2_part_number=part_number,
                        variant_values={})
    )

    for station_cfg, substeps in zip(STATIONS, SUBSTEPS_BY_OP):
        _build_instruction_set(
            session, product, title=station_cfg["title"], op_code=station_cfg["op_code"],
            substeps=substeps,
        )

    job_number = f"GATE3-{tag}-01"
    line_item = JB2OrderLineItem(
        id=uuid.uuid4(), jb2_id=f"gate3-li-{tag}", part_number=part_number,
        description="Gate3 walk line item", qty=1,
        payload={"jobNumber": job_number, "orderNumber": f"GATE3-{tag}"}, **_mirror_common(now),
    )
    session.add(line_item)
    session.flush()

    # int32-safe (JB2's workCenter is int32) + collision-unlikely per run --
    # a timestamp-based id would overflow int32, so use a small random base.
    wc_id_base = (uuid.uuid4().int % 90_000) * 10 + 100_000
    for seq, station_cfg in enumerate(STATIONS, start=1):
        session.add(
            JB2OrderRouting(
                id=uuid.uuid4(), jb2_id=f"gate3-routing-{tag}-{seq}", jb2_line_item_id=line_item.id,
                seq=seq, operation_code=station_cfg["op_code"], description=station_cfg["title"],
                work_center_code=station_cfg["wc"], payload={}, **_mirror_common(now),
            )
        )
        # Mirror the work center too -- app/outbox/payloads.py's
        # _work_center_int resolves TimeTicketDetailCreate.workCenter (int32)
        # via this table's `jb2_id` cast to int; without it the write-back
        # safely degrades to workCenter=None (not a bug), but seeding it
        # gives fuller gate evidence of the real write-back shape.
        # jb2_id must be numeric-string (int(jb2_id)) -- unlike every other
        # mirror row's jb2_id, which is opaque text.
        session.add(
            JB2WorkCenter(
                id=uuid.uuid4(), jb2_id=str(wc_id_base + seq), code=station_cfg["wc"],
                name=station_cfg["name"], payload={}, **_mirror_common(now),
            )
        )
    session.flush()

    # The one call that builds WorkOrder + frozen 3-op ExecutionPlan + Unit,
    # exactly the production order-ingestion path (app/sync/hooks.py calls
    # this same function on order_line_item_new).
    work_order = workorders.create_from_line_item(session, line_item)
    if work_order is None:
        raise GateWalkError("create_from_line_item returned None -- part number failed to map")
    session.flush()
    if work_order.status != "ready":
        raise GateWalkError(
            f"expected work_order.status == 'ready' after seeding (all 3 ops bound), "
            f"got {work_order.status!r} -- a binding/library-publish defect, not a harness bug"
        )

    plan_ops = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == work_order.id)
        .order_by(PlanOperation.seq)
    ).all()
    if len(plan_ops) != 3 or any(op.blocked for op in plan_ops):
        raise GateWalkError(f"expected 3 unblocked plan_operations, got {[(o.seq, o.blocked) for o in plan_ops]}")

    unit = session.scalars(select(Unit).where(Unit.work_order_id == work_order.id)).one()

    # Stations (kiosk enrollment) -- one per work center.
    stations = {}
    for station_cfg in STATIONS:
        station, token = service.create_station(
            session, name=f"{station_cfg['name']} {tag}", work_center_code=station_cfg["wc"],
        )
        stations[station_cfg["wc"]] = (station, token)

    # Employee-linked operator (jb2_employee_id so the write-back has a real
    # employeeCode) -- also holds the lead role, per the task brief ("1
    # operator ... with badge + lead role"). A signoff substep's second-badge
    # rule (state-machine.md §4 O8) forbids self-authorization though (see
    # app/auth/service.py's second_badge -- "authorizer cannot be the acting
    # operator"), which is a real domain rule this harness must respect, not
    # route around -- so a second, harness-only "authorizer" operator badges
    # in only to countersign the QC signoff. It never drives the unit itself.
    jb2_emp = JB2Employee(
        id=uuid.uuid4(), jb2_id=f"gate3-emp-{tag}", employee_code="4200", name="Gate3 Operator",
        active=True, payload={}, **_mirror_common(now),
    )
    session.add(jb2_emp)
    session.flush()
    operator = service.create_operator(
        session, display_name=f"Gate3 Operator {tag}", roles=["operator", "lead"],
        jb2_employee_id=jb2_emp.id,
    )
    authorizer = service.create_operator(
        session, display_name=f"Gate3 QC Authorizer {tag}", roles=["lead"],
    )

    failure_code = library.upsert_failure_code(
        session, None, f"GATE3-{tag}", "Gate3 walk sample failure code", who="gate3-walk",
    )

    session.commit()

    return {
        "product": product, "work_order": work_order, "plan_ops": plan_ops, "unit": unit,
        "stations": stations, "operator": operator, "authorizer": authorizer,
        "failure_code": failure_code, "job_number": job_number, "part_number": part_number,
        "line_item": line_item,
    }


# -- HTTP walk -----------------------------------------------------------------


def build_test_client(engine):
    """Real app.main.app (every Phase-3 router already mounted there), get_session
    overridden to bind to our engine -- same dependency-override pattern as
    every tests/integration/test_*.py fixture in this repo."""
    from app.main import app as fastapi_app  # import here: after MES_SECRET_KEY is set

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_session] = _override
    return TestClient(fastapi_app, follow_redirects=False), session_factory


def run_walk(client: TestClient, seeded: dict, tag: str, defects: list[str]) -> dict:
    """Drives the unit through kit-up + all 3 stations over real HTTP. Returns
    the per-station visit log for the report."""
    work_order = seeded["work_order"]
    unit = seeded["unit"]
    plan_ops = seeded["plan_ops"]
    stations = seeded["stations"]
    operator = seeded["operator"]
    authorizer = seeded["authorizer"]

    box_qr = f"BOX:gate3-{tag}"
    serial = f"GATE3-SN-{tag}"

    # -- kit-up (POST /admin/kitup -- real HTTP, no station token needed) --
    resp = client.post(
        "/admin/kitup",
        data={
            "work_order_id": str(work_order.id), "unit_id": str(unit.id), "box_qr": box_qr,
            "serial": serial, "operator_badge": operator.badge_qr,
        },
    )
    if resp.status_code != 303 or "success" not in resp.headers.get("location", ""):
        raise GateWalkError(f"kit-up failed: {resp.status_code} {resp.headers.get('location')}")
    kitup_result = {"status_code": resp.status_code, "location": resp.headers["location"],
                     "box_qr": box_qr, "serial": serial}

    visits = []
    for i, (station_cfg, plan_op) in enumerate(zip(STATIONS, plan_ops)):
        station, token = stations[station_cfg["wc"]]
        headers = {"X-Station-Token": token}
        visit: dict = {"seq": plan_op.seq, "work_center": station_cfg["wc"], "station_name": station.name}

        # badge in at this station
        r = client.post("/auth/badge", json={"payload": operator.badge_qr}, headers=headers)
        if r.status_code != 200:
            raise GateWalkError(f"badge-in failed at {station_cfg['wc']}: {r.status_code} {r.text}")
        visit["badge_status"] = r.status_code

        # scan the box -- accept at this station (S6), closing any open transit
        r = client.post("/scan", json={"payload": box_qr}, headers=headers)
        body = r.json()
        visit["scan_status"] = r.status_code
        visit["scan_code"] = body.get("code")
        visit["work_session_id"] = body.get("context", {}).get("work_session_id")
        if r.status_code != 200 or body.get("code") != "accepted":
            raise GateWalkError(f"scan not accepted at {station_cfg['wc']}: {body}")

        # complete every substep for this op's one frozen step (seq=1)
        substep_results = []
        substeps = SUBSTEPS_BY_OP[i]
        for sub_seq, sub in enumerate(substeps, start=1):
            request_id = f"gate3-{tag}-op{plan_op.seq}-sub{sub_seq}"
            if sub["type"] in ("action", "inspection"):
                r = client.post(
                    f"/station/units/{unit.id}/substeps/1/{sub_seq}/complete",
                    data={"request_id": request_id},
                    headers=headers,
                )
            elif sub["type"] == "measurement":
                # in-tolerance value (nominal 0.5, tol +-0.01)
                r = client.post(
                    f"/station/units/{unit.id}/substeps/1/{sub_seq}/measurement",
                    data={"value": "0.500", "request_id": request_id},
                    headers=headers,
                )
            elif sub["type"] == "signoff":
                r = client.post(
                    f"/station/units/{unit.id}/substeps/1/{sub_seq}/signoff",
                    data={"override_badge": authorizer.badge_qr, "request_id": request_id},
                    headers=headers,
                )
            else:
                raise GateWalkError(f"gate3_walk.py doesn't know substep type {sub['type']!r}")

            location = r.headers.get("location", "")
            ok = r.status_code == 303 and "error" not in location
            substep_results.append(
                {"seq": sub_seq, "type": sub["type"], "title": sub["title"],
                 "status_code": r.status_code, "location": location, "ok": ok}
            )
            if not ok:
                raise GateWalkError(
                    f"substep {sub_seq} ({sub['type']}) failed at {station_cfg['wc']}: "
                    f"{r.status_code} {location}"
                )
        visit["substeps"] = substep_results

        # finish the operation
        r = client.post(
            f"/operations/{plan_op.id}/finish", json={"unit_id": str(unit.id)}, headers=headers,
        )
        fbody = r.json()
        visit["finish_status"] = r.status_code
        visit["finish_body"] = fbody
        if r.status_code != 200 or fbody.get("code") != "finished":
            raise GateWalkError(f"finish failed at {station_cfg['wc']}: {r.status_code} {fbody}")
        expected_unit_status = "done" if i == len(STATIONS) - 1 else "in_transit"
        if fbody.get("unit_status") != expected_unit_status:
            defects.append(
                f"finish at {station_cfg['wc']}: expected unit_status={expected_unit_status!r}, "
                f"got {fbody.get('unit_status')!r}"
            )
        visits.append(visit)

    return {"kitup": kitup_result, "visits": visits, "box_qr": box_qr, "serial": serial}


# -- outbox drain + CR-010 check -----------------------------------------------


def drain_and_check(session_factory) -> dict:
    state = seed_state()
    fake_app = create_fake_jb2(state)
    transport = TestClient(fake_app)._transport
    jb2_client = Jb2Client(
        "https://api-jb2.example.com", "https://auth-jb2.example.com",
        "gate3-client-id", "gate3-client-secret", transport=transport, sleeper=lambda s: None,
    )
    drain_session = session_factory()
    try:
        outcomes = drainer.drain_once(drain_session, jb2_client)
    finally:
        drain_session.close()
        jb2_client.close()

    writes = state["received_writes"]
    paths = {w["path"] for w in writes}
    cr010_ok = paths <= {"/time-tickets", "/time-ticket-details"} and not any(
        w["path"].startswith("/order-routings") for w in writes
    )
    return {"outcomes": outcomes, "writes": writes, "paths": sorted(paths), "cr010_ok": cr010_ok}


# -- §9.1-9.4 queryability dump -------------------------------------------------


def gather_query_evidence(session: Session, seeded: dict, walk: dict) -> dict:
    unit = session.get(Unit, seeded["unit"].id)
    work_order = session.get(WorkOrder, seeded["work_order"].id)
    box = session.scalars(select(BuildBox).where(BuildBox.qr_payload == walk["box_qr"])).one()
    plan_ops = session.scalars(
        select(PlanOperation).where(PlanOperation.work_order_id == work_order.id).order_by(PlanOperation.seq)
    ).all()

    scans = session.scalars(select(Scan).where(Scan.box_id == box.id).order_by(Scan.scanned_at)).all()
    transits = session.scalars(
        select(Transit).where(Transit.unit_id == unit.id).order_by(Transit.departed_at)
    ).all()
    work_sessions = session.scalars(
        select(WorkSession).where(WorkSession.unit_id == unit.id).order_by(WorkSession.started_at)
    ).all()
    step_execs = session.scalars(
        select(StepExecution).where(StepExecution.unit_id == unit.id)
    ).all()
    step_exec_ids = [se.id for se in step_execs]
    substep_execs = (
        session.scalars(
            select(SubstepExecution).where(SubstepExecution.step_execution_id.in_(step_exec_ids))
        ).all()
        if step_exec_ids else []
    )
    substep_exec_ids = [se.id for se in substep_execs]
    measurements = (
        session.scalars(
            select(Measurement).where(Measurement.substep_execution_id.in_(substep_exec_ids))
        ).all()
        if substep_exec_ids else []
    )
    signoffs = [se for se in substep_execs if se.type == "signoff" and se.status == "done"]
    scrap_events = session.scalars(select(ScrapEvent).where(ScrapEvent.unit_id == unit.id)).all()

    # events.timeline() as documented (unit-entity-only rows: unit.moved/unit.done)
    unit_only_timeline = events.timeline(session, unit_id=unit.id)

    # full cross-entity journey timeline for this unit (its own id + every
    # entity that references it: scans, sessions, substeps, measurements) --
    # events.timeline's own docstring flags this as the caller's job, not
    # something unit_id= alone does.
    all_ids = [unit.id, box.id] + [s.id for s in scans] + [ws.id for ws in work_sessions] \
        + substep_exec_ids + [m.id for m in measurements]
    full_timeline = session.scalars(
        select(Event).where(Event.entity_id.in_(all_ids)).order_by(Event.at.asc(), Event.id.asc())
    ).all()

    return {
        "unit": unit, "work_order": work_order, "box": box, "plan_ops": plan_ops,
        "scans": scans, "transits": transits, "work_sessions": work_sessions,
        "substep_execs": substep_execs, "measurements": measurements, "signoffs": signoffs,
        "scrap_events": scrap_events, "unit_only_timeline": unit_only_timeline,
        "full_timeline": full_timeline,
    }


# -- report rendering -----------------------------------------------------------


def _fmt_event(e: Event) -> str:
    return f"  [{e.at}] {e.verb:<22} entity={e.entity_kind}:{e.entity_id} after={e.after}"


def render_report(*, tag: str, db_notices: list[str], schema_note: str, using_postgres: bool,
                   seeded: dict, walk: dict, drain: dict, evidence: dict, defects: list[str],
                   error: str | None) -> str:
    lines: list[str] = []
    a = lines.append
    a("# Gate 3 walk output")
    a("")
    a(f"Run tag: `{tag}`")
    a(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    a(f"Database: {'PostgreSQL 16' if using_postgres else 'sqlite fallback (Postgres unreachable)'}")
    for n in db_notices:
        a(f"- {n}")
    a(f"- {schema_note}")
    a("")

    if seeded:
        a("## 1. Seed summary (real domain layer: app.domain.library / app.domain.workorders)")
        a("")
        a(f"- Product: {seeded['product'].name} ({seeded['product'].id})")
        a(f"- JB2 part number: {seeded['part_number']}  |  job number: {seeded['job_number']}")
        a(f"- Work order: {seeded['work_order'].id} status={seeded['work_order'].status}")
        a(f"- Unit: {seeded['unit'].id} unit_no={seeded['unit'].unit_no}")
        a(f"- Plan operations ({len(seeded['plan_ops'])}):")
        for op in seeded["plan_ops"]:
            a(f"    seq={op.seq} {op.title!r} instruction_set={op.instruction_set_id} "
              f"v{op.instruction_version} blocked={op.blocked}")
        a(f"- Operator (jb2 employee-linked, roles={seeded['operator'].roles}): "
          f"{seeded['operator'].display_name} ({seeded['operator'].id})")
        a(f"- QC signoff authorizer (roles={seeded['authorizer'].roles}): "
          f"{seeded['authorizer'].display_name} ({seeded['authorizer'].id})")
        a(f"- Failure code seeded: {seeded['failure_code'].code} ({seeded['failure_code'].id})")
        a("")

    if error:
        a("## FATAL ERROR -- walk aborted")
        a("")
        a("```")
        a(error)
        a("```")
        a("")

    if walk:
        a("## 2. Kit-up")
        a("")
        k = walk["kitup"]
        a(f"- POST /admin/kitup -> {k['status_code']} {k['location']}")
        a(f"- box_qr={k['box_qr']}  serial={k['serial']}")
        a("")

        a("## 3. Station-by-station walk (real HTTP: badge -> scan -> substeps -> finish)")
        a("")
        for v in walk["visits"]:
            a(f"### Op seq={v['seq']} ({v['work_center']}, station {v['station_name']})")
            a(f"- badge-in: {v['badge_status']}")
            a(f"- scan: {v['scan_status']} code={v['scan_code']!r} work_session_id={v['work_session_id']}")
            for s in v["substeps"]:
                a(f"- substep {s['seq']} [{s['type']}] {s['title']!r}: {s['status_code']} ok={s['ok']}")
            fb = v["finish_body"]
            a(f"- finish: {v['finish_status']} code={fb.get('code')!r} "
              f"unit_status={fb.get('unit_status')!r} outbox_ids={fb.get('outbox_ids')}")
            a("")

    if evidence:
        u = evidence["unit"]
        a("## 4. Unit final state")
        a("")
        a(f"- unit.status = {u.status!r}")
        a(f"- unit.serial_number = {u.serial_number!r}")
        a(f"- unit.first_pass = {u.first_pass!r}  rework_count={u.rework_count}")
        a(f"- unit.completed_at = {u.completed_at}")
        a("")

    if drain:
        a("## 5. Outbox drain to fake-JB2")
        a("")
        a(f"- drain outcomes: {drain['outcomes']}")
        a(f"- distinct write paths received: {drain['paths']}")
        a("- writes:")
        for w in drain["writes"]:
            a(f"    {w['path']}  body={w['body']}")
        verdict = "PASS" if drain["cr010_ok"] else "FAIL"
        a(f"- **CR-010 check (zero /order-routings PATCH, only time-tickets/time-ticket-details): {verdict}**")
        a("")

    if evidence:
        a("## 6. §9.1-9.4 data queryability (DD §9, MES_Design_Document.md)")
        a("")
        wo = evidence["work_order"]
        box = evidence["box"]
        u = evidence["unit"]
        a("### §9.1 Identity & traceability")
        a(f"- work_order {wo.id} <-> jb2_line_item {wo.jb2_line_item_id}")
        a(f"- unit serial: {u.serial_number}  box: {box.id} qr={box.qr_payload}")
        a("- instruction sets/version per op: " +
          ", ".join(f"seq{op.seq}:{op.instruction_set_id}/v{op.instruction_version}"
                     for op in evidence["plan_ops"]))
        a("")
        a("### §9.2 Location & movement")
        a(f"- scans recorded: {len(evidence['scans'])}")
        for s in evidence["scans"]:
            a(f"    scan {s.id}: station={s.station_id} result={s.result} override_by={s.override_by} at={s.scanned_at}")
        a(f"- transit hops: {len(evidence['transits'])}")
        for t in evidence["transits"]:
            a(f"    transit {t.id}: {t.from_station_id} -> {t.to_station_id} "
              f"seconds={t.seconds} arrived={t.arrived_at is not None}")
        a(f"- box current location (station_id): {box.current_station_id}  current_unit: {box.current_unit_id}")
        a("")
        a("### §9.3 Time")
        a(f"- work sessions: {len(evidence['work_sessions'])}")
        for ws in evidence["work_sessions"]:
            a(f"    session {ws.id}: op={ws.plan_operation_id} kind={ws.kind} "
              f"started={ws.started_at} ended={ws.ended_at} close_reason={ws.close_reason}")
        started = sum(1 for se in evidence["substep_execs"] if se.started_at is not None)
        completed = sum(1 for se in evidence["substep_execs"] if se.completed_at is not None)
        a(f"- substep executions: {len(evidence['substep_execs'])} "
          f"(started={started}, completed={completed})")
        a("")
        a("### §9.4 Quality & measurements")
        a(f"- measurements recorded: {len(evidence['measurements'])}")
        for m in evidence["measurements"]:
            a(f"    {m.name}: value={m.value} nominal={m.nominal} in_tolerance={m.in_tolerance} "
              f"gauge_id={m.gauge_id}")
        a(f"- signoffs completed: {len(evidence['signoffs'])}")
        a(f"- first_pass flag: {u.first_pass}")
        a(f"- scrap events: {len(evidence['scrap_events'])} (expected 0 on this happy path)")
        a("")
        a("### Unit event timeline")
        a("`events.timeline(session, unit_id=unit.id)` (unit-entity-only rows, per its own docstring):")
        for e in evidence["unit_only_timeline"]:
            a(_fmt_event(e))
        a("")
        a("Full cross-entity journey timeline (unit + its scans/sessions/substeps/measurements):")
        for e in evidence["full_timeline"]:
            a(_fmt_event(e))
        a("")

    if defects:
        a("## Defects / anomalies")
        a("")
        for d in defects:
            a(f"- {d}")
        a("")

    overall_pass = (
        error is None
        and not defects
        and bool(drain)
        and drain.get("cr010_ok", False)
        and bool(evidence)
        and evidence["unit"].status == "done"
    )
    a(f"## GATE 3 WALK: {'PASS' if overall_pass else 'FAIL'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLAlchemy DB URL (overrides DATABASE_URL / the pg default)")
    parser.add_argument("--tag", help="Run tag for uniquely-namespaced seed rows (default: fresh per run)")
    args = parser.parse_args()

    tag = args.tag or _fresh_tag()
    engine, using_postgres, db_notices = resolve_engine(args.db)
    schema_note = ensure_schema(engine, using_postgres)

    seeded: dict = {}
    walk: dict = {}
    drain: dict = {}
    evidence: dict = {}
    defects: list[str] = []
    error: str | None = None

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as seed_session:
            seeded = seed(seed_session, tag)

        client, session_factory = build_test_client(engine)
        with client:
            walk = run_walk(client, seeded, tag, defects)

        drain = drain_and_check(session_factory)

        with session_factory() as report_session:
            evidence = gather_query_evidence(report_session, seeded, walk)
    except Exception:  # noqa: BLE001 -- surfaced verbatim in the report, not swallowed
        error = traceback.format_exc()

    report = render_report(
        tag=tag, db_notices=db_notices, schema_note=schema_note, using_postgres=using_postgres,
        seeded=seeded, walk=walk, drain=drain, evidence=evidence, defects=defects, error=error,
    )

    print(report)
    out_path = REPO_ROOT / "docs" / "gates" / "phase3_walk_output.md"
    out_path.write_text(report)
    print(f"(report also written to {out_path})")

    return 0 if error is None and "GATE 3 WALK: PASS" in report else 1


if __name__ == "__main__":
    raise SystemExit(main())
