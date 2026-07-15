"""P4-09 perf indexes: two EXPLAIN-proven hot-path indexes from the Gate 4
load check (docs/gates/phase4_perf.md).

- ix_units_active_status: the dashboard board's outer query filters units by
  status alone (no work_order_id), so the existing (work_order_id, status)
  composite can't serve it. Partial index on the three board-active statuses.
- ix_transits_unit_station_arrived: a correlated subplan in
  v_queue_time_by_station scans transits per unit; this composite serves it.

Both Postgres-only refinements (partial index needs the WHERE clause); on
SQLite the partial index is created as a plain 3-status-unaware index is
skipped -- the metrics/board tests run on SQLite where table sizes are tiny.

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"
    if is_postgres:
        op.execute(
            "CREATE INDEX ix_units_active_status ON units (status) "
            "WHERE status IN ('queued', 'at_station', 'in_transit')"
        )
    else:
        op.create_index("ix_units_active_status", "units", ["status"])
    op.create_index(
        "ix_transits_unit_station_arrived",
        "transits",
        ["unit_id", "to_station_id", "arrived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_transits_unit_station_arrived", table_name="transits")
    op.drop_index("ix_units_active_status", table_name="units")
