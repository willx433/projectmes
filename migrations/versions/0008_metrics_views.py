"""metrics SQL views (P4-04)

DD §13.2/§9.11: "All metrics are SQL views over the §10 tables -- no
separate analytics store in v1." Creates 9 views (7 named DD metrics +
2 supporting views -- `v_fpy_operation` splits FPY unit/operation per the
task brief, and `v_work_session_minutes` is a shared duration-net-of-pauses
helper reused by `v_actual_vs_estimate` and `v_rework_hours_pct` rather than
duplicating that expression twice):

  - v_fpy_unit               -- first-pass yield, unit level
  - v_fpy_operation          -- first-pass yield, operation level
  - v_scrap_pareto           -- scrap events by cause code
  - v_throughput_daily       -- units done/day by product
  - v_work_session_minutes   -- (helper) per-session actual minutes, net of pauses
  - v_actual_vs_estimate     -- actual vs. estimated minutes per operation instance
  - v_queue_time_by_station  -- session-start minus most recent transit arrival
  - v_rework_hours_pct       -- rework minutes vs. total minutes (single row)
  - v_wip_age                -- hours since work-order creation, units not done/scrapped

All definitions live in `app/domain/metrics.py` (dialect-branched: every
view has a working sqlite variant using julianday()/date() in place of
Postgres's EXTRACT(EPOCH ...)/date_trunc/CURRENT_TIMESTAMP interval math --
none are Postgres-only) so the exact same SQL is exercised by
tests/integration/test_metrics.py (raw CREATE VIEW against a fresh sqlite
engine, since this repo's integration tests build schema via
Base.metadata.create_all rather than running Alembic in-process) and by
this migration for real Postgres deployments.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-15

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.domain import metrics

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"
    metrics.create_views(op.get_bind(), is_postgres)


def downgrade() -> None:
    metrics.drop_views(op.get_bind())
