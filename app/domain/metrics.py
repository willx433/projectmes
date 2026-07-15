"""Metrics SQL views (P4-04, DD §13.2/§9.11: "all metrics are SQL views over
the §10 tables -- no separate analytics store").

The view definitions live here, dialect-branched (`is_postgres`), so the
exact same SQL is used by:
  - migrations/versions/0008_metrics_views.py (Alembic, real deployments)
  - tests/integration/test_metrics.py (raw `CREATE VIEW` against a fresh
    sqlite engine -- the repo's integration tests build schema via
    `Base.metadata.create_all`, not by running Alembic in-process, so the
    views need the same create-them-directly treatment)
  - app/api/metrics.py (queries them; never redefines them)

Every view here has a working sqlite variant (julianday()/date() arithmetic
in place of Postgres's EXTRACT(EPOCH ...)/date_trunc/CURRENT_TIMESTAMP
interval math) -- none are Postgres-only, so nothing is skipped in the
sqlite test run. Two views (`v_actual_vs_estimate`, `v_rework_hours_pct`)
are built on top of a shared helper view (`v_work_session_minutes`, net of
pauses) rather than duplicating that duration-minus-pauses expression twice
-- still one `CREATE VIEW` per named metric, per the task brief.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Creation order matters: v_work_session_minutes must exist before
# v_actual_vs_estimate/v_rework_hours_pct (both SELECT FROM it). Drop order
# is the reverse.
VIEW_NAMES: tuple[str, ...] = (
    "v_fpy_unit",
    "v_fpy_operation",
    "v_scrap_pareto",
    "v_throughput_daily",
    "v_work_session_minutes",
    "v_actual_vs_estimate",
    "v_queue_time_by_station",
    "v_rework_hours_pct",
    "v_wip_age",
)


def _epoch_seconds(is_postgres: bool, end_expr: str, start_expr: str) -> str:
    """`end_expr - start_expr` in seconds, portable."""
    if is_postgres:
        return f"EXTRACT(EPOCH FROM ({end_expr} - {start_expr}))"
    return f"((julianday({end_expr}) - julianday({start_expr})) * 86400.0)"


def _day_expr(is_postgres: bool, col: str) -> str:
    """`col` truncated to a day -- sqlite's dynamic-typed CAST(... AS DATE)
    doesn't truncate a datetime string, so this needs the dialect branch."""
    return f"({col})::date" if is_postgres else f"date({col})"


def _hours_since(is_postgres: bool, col: str) -> str:
    if is_postgres:
        return f"EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - {col}))/3600.0"
    return f"((julianday(CURRENT_TIMESTAMP) - julianday({col})) * 24.0)"


def view_definitions(is_postgres: bool) -> dict[str, str]:
    """`view_name -> SELECT ...` body (no `CREATE VIEW x AS` prefix)."""
    session_seconds = _epoch_seconds(is_postgres, "ws.ended_at", "ws.started_at")
    pause_seconds = _epoch_seconds(is_postgres, "ended_at", "started_at")
    # Repeated (not aliased-and-reused) because SQL forbids referencing one
    # SELECT-list alias from another expression in the same SELECT list.
    queue_arrival_subq = (
        "(SELECT MAX(t.arrived_at) FROM transits t "
        "WHERE t.unit_id = ws.unit_id AND t.to_station_id = ws.station_id "
        "AND t.arrived_at <= ws.started_at)"
    )
    queue_seconds = _epoch_seconds(is_postgres, "ws.started_at", queue_arrival_subq)

    return {
        # -- FPY, unit level (DD §13.2/§9.11) --------------------------------
        "v_fpy_unit": """
            SELECT u.id AS unit_id, u.work_order_id, wo.product_id, p.name AS product_name,
                   u.status, u.first_pass, u.completed_at
            FROM units u
            JOIN work_orders wo ON wo.id = u.work_order_id
            JOIN products p ON p.id = wo.product_id
        """,
        # -- FPY, operation level: a unit is "clean" at an operation unless a
        # rework failure (in-place or send-back) was recorded against it --
        # use_as_is/scrap are different categories, not counted against FPY
        # here (use_as_is is "no rework happened"; scrap is its own metric).
        "v_fpy_operation": """
            SELECT po.id AS plan_operation_id, po.work_order_id, po.seq, po.operation_code,
                   po.title, se.unit_id,
                   CASE WHEN EXISTS (
                     SELECT 1 FROM failures f
                     WHERE f.plan_operation_id = po.id AND f.unit_id = se.unit_id
                       AND f.disposition IN ('rework_in_place', 'rework_to_op')
                   ) THEN 0 ELSE 1 END AS first_pass_at_op
            FROM plan_operations po
            JOIN (SELECT DISTINCT plan_operation_id, unit_id FROM step_executions) se
              ON se.plan_operation_id = po.id
        """,
        # -- scrap Pareto -----------------------------------------------------
        "v_scrap_pareto": """
            SELECT COALESCE(fc.code, 'unknown') AS cause_code,
                   COALESCE(fc.label, 'Unknown') AS cause_label,
                   se.id AS scrap_event_id, se.unit_id, se.material_value_est, se.created_at
            FROM scrap_events se
            JOIN failures f ON f.id = se.failure_id
            LEFT JOIN failure_codes fc ON fc.id = f.failure_code_id
        """,
        # -- throughput (units done/day by product) --------------------------
        "v_throughput_daily": f"""
            SELECT u.id AS unit_id, wo.product_id, p.name AS product_name, u.completed_at,
                   {_day_expr(is_postgres, "u.completed_at")} AS done_day
            FROM units u
            JOIN work_orders wo ON wo.id = u.work_order_id
            JOIN products p ON p.id = wo.product_id
            WHERE u.status = 'done' AND u.completed_at IS NOT NULL
        """,
        # -- shared helper: per-session actual minutes, net of pauses --------
        "v_work_session_minutes": f"""
            SELECT ws.id AS work_session_id, ws.unit_id, ws.plan_operation_id, ws.station_id,
                   ws.kind,
                   ({session_seconds} - COALESCE(sp.paused_seconds, 0)) / 60.0 AS actual_minutes
            FROM work_sessions ws
            LEFT JOIN (
                SELECT work_session_id, SUM({pause_seconds}) AS paused_seconds
                FROM session_pauses
                WHERE ended_at IS NOT NULL
                GROUP BY work_session_id
            ) sp ON sp.work_session_id = ws.id
            WHERE ws.ended_at IS NOT NULL
        """,
        # -- actual vs. estimated minutes per operation instance -------------
        "v_actual_vs_estimate": """
            SELECT po.id AS plan_operation_id, po.work_order_id, po.seq, po.operation_code,
                   po.title, po.est_minutes,
                   COUNT(DISTINCT wsm.unit_id) AS units_worked,
                   SUM(wsm.actual_minutes) AS actual_minutes_total,
                   po.est_minutes * COUNT(DISTINCT wsm.unit_id) AS est_minutes_total
            FROM plan_operations po
            JOIN v_work_session_minutes wsm ON wsm.plan_operation_id = po.id
            GROUP BY po.id, po.work_order_id, po.seq, po.operation_code, po.title, po.est_minutes
        """,
        # -- queue time by station: session start minus the most recent
        # transit arrival at that station for that unit (NULL when there's
        # no preceding transit -- e.g. the unit's very first operation).
        "v_queue_time_by_station": f"""
            SELECT ws.id AS work_session_id, ws.station_id, s.name AS station_name,
                   ws.unit_id, ws.started_at,
                   {queue_arrival_subq} AS arrived_at,
                   {queue_seconds} AS queue_seconds
            FROM work_sessions ws
            JOIN stations s ON s.id = ws.station_id
        """,
        # -- rework hours as % of total (single-row view; pct computed by
        # the caller from the two totals to avoid a divide-by-zero in SQL).
        "v_rework_hours_pct": """
            SELECT
                SUM(CASE WHEN kind = 'rework' THEN actual_minutes ELSE 0 END) AS rework_minutes,
                SUM(actual_minutes) AS total_minutes
            FROM v_work_session_minutes
        """,
        # -- WIP age: hours since the work order was created, for units not
        # yet done/scrapped (units carry no created_at of their own -- the
        # work order's is the closest DD-schema proxy for "when this unit
        # entered the pipeline").
        "v_wip_age": f"""
            SELECT u.id AS unit_id, u.unit_no, u.work_order_id, wo.product_id,
                   p.name AS product_name, u.status,
                   {_hours_since(is_postgres, "wo.created_at")} AS age_hours
            FROM units u
            JOIN work_orders wo ON wo.id = u.work_order_id
            JOIN products p ON p.id = wo.product_id
            WHERE u.status NOT IN ('done', 'scrapped')
        """,
    }


def create_views(conn: Connection, is_postgres: bool) -> None:
    for name, select_sql in view_definitions(is_postgres).items():
        conn.execute(text(f"CREATE VIEW {name} AS {select_sql}"))


def drop_views(conn: Connection) -> None:
    for name in reversed(VIEW_NAMES):
        conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
