"""GET /dashboard/metrics (P4-04, DD §13.2/§9.11).

Queries the SQL views defined in `app/domain/metrics.py` / created by
migrations/versions/0008_metrics_views.py -- no aggregation logic lives in
Python beyond simple percentage/bucket math the views themselves don't do
(dividing two totals, sorting a handful of rows into age buckets). Renders
tables + inline CSS bars, no chart library, per the task brief (self-
contained, internal network).

`?days=N` (default 30) windows the metrics that carry their own timestamp
(FPY-unit, scrap Pareto, throughput, queue time). FPY-by-operation and
actual-vs-estimate are per-operation running totals with no timestamp of
their own on the underlying view, so they aren't windowed -- same
DD §13.2 phrasing ("by product/operation") as a running comparison, not a
dated one. WIP age is inherently point-in-time (current pipeline state).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.db import get_session
from app.domain.models_jb2 import DisplayCache

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

WIP_AGE_BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("<24h", 0, 24),
    ("24-48h", 24, 48),
    ("48-96h", 48, 96),
    ("96h+", 96, None),
)


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(days, 1))


def fpy_by_product(session: Session, cutoff: datetime) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT product_name,
                   COUNT(*) AS units_done,
                   SUM(CASE WHEN first_pass THEN 1 ELSE 0 END) AS units_first_pass
            FROM v_fpy_unit
            WHERE status = 'done' AND completed_at >= :cutoff
            GROUP BY product_name
            ORDER BY product_name
            """
        ),
        {"cutoff": cutoff},
    ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["fpy_pct"] = (d["units_first_pass"] / d["units_done"] * 100) if d["units_done"] else None
        out.append(d)
    return out


def fpy_by_operation(session: Session) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT operation_code, title,
                   COUNT(*) AS units_reached,
                   SUM(first_pass_at_op) AS units_clean
            FROM v_fpy_operation
            GROUP BY operation_code, title
            ORDER BY operation_code
            """
        )
    ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["fpy_pct"] = (d["units_clean"] / d["units_reached"] * 100) if d["units_reached"] else None
        out.append(d)
    return out


def scrap_pareto(session: Session, cutoff: datetime) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT cause_code, cause_label, COUNT(*) AS scrap_count,
                   SUM(COALESCE(material_value_est, 0)) AS material_value_total
            FROM v_scrap_pareto
            WHERE created_at >= :cutoff
            GROUP BY cause_code, cause_label
            ORDER BY scrap_count DESC
            """
        ),
        {"cutoff": cutoff},
    ).mappings().all()
    return [dict(r) for r in rows]


def throughput_daily(session: Session, cutoff: datetime) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT product_name, done_day, COUNT(*) AS units_done
            FROM v_throughput_daily
            WHERE completed_at >= :cutoff
            GROUP BY product_name, done_day
            ORDER BY done_day DESC, product_name
            """
        ),
        {"cutoff": cutoff},
    ).mappings().all()
    return [dict(r) for r in rows]


def actual_vs_estimate(session: Session) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT operation_code, title, est_minutes, units_worked,
                   actual_minutes_total, est_minutes_total
            FROM v_actual_vs_estimate
            ORDER BY operation_code
            """
        )
    ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        if d["actual_minutes_total"] is not None and d["est_minutes_total"] is not None:
            d["delta_minutes"] = d["actual_minutes_total"] - d["est_minutes_total"]
        else:
            d["delta_minutes"] = None
        out.append(d)
    return out


def queue_time_by_station(session: Session, cutoff: datetime) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT station_name, AVG(queue_seconds) AS avg_queue_seconds, COUNT(*) AS sessions
            FROM v_queue_time_by_station
            WHERE arrived_at IS NOT NULL AND started_at >= :cutoff
            GROUP BY station_name
            ORDER BY avg_queue_seconds DESC
            """
        ),
        {"cutoff": cutoff},
    ).mappings().all()
    return [dict(r) for r in rows]


def rework_hours_pct(session: Session) -> dict:
    row = session.execute(
        text("SELECT rework_minutes, total_minutes FROM v_rework_hours_pct")
    ).mappings().first() or {}
    rework = row.get("rework_minutes") or 0
    total = row.get("total_minutes") or 0
    return {
        "rework_minutes": rework,
        "total_minutes": total,
        "rework_pct": (rework / total * 100) if total else None,
    }


def wip_age_histogram(session: Session) -> list[dict]:
    ages = session.execute(text("SELECT age_hours FROM v_wip_age")).scalars().all()
    out = []
    for label, lo, hi in WIP_AGE_BUCKETS:
        count = sum(1 for h in ages if h is not None and h >= lo and (hi is None or h < hi))
        out.append({"bucket": label, "count": count})
    return out


def jb2_schedule_overlay(session: Session) -> dict | None:
    row = session.get(DisplayCache, "jb2-schedule")
    if row is None:
        return None
    return {"fetched_at": row.fetched_at.isoformat(), "payload": row.payload}


@router.get("/dashboard/metrics")
def dashboard_metrics(request: Request, days: int = 30, session: Session = Depends(get_session)):
    cutoff = _cutoff(days)
    context = {
        "days": days,
        "fpy_by_product": fpy_by_product(session, cutoff),
        "fpy_by_operation": fpy_by_operation(session),
        "scrap_pareto": scrap_pareto(session, cutoff),
        "throughput_daily": throughput_daily(session, cutoff),
        "actual_vs_estimate": actual_vs_estimate(session),
        "queue_time_by_station": queue_time_by_station(session, cutoff),
        "rework_hours_pct": rework_hours_pct(session),
        "wip_age_histogram": wip_age_histogram(session),
        "jb2_schedule": jb2_schedule_overlay(session),
    }
    return templates.TemplateResponse(request, "dashboard/metrics.html", context)
