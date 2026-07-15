"""execution core (P2-10)

Creates the `work_orders` / `units` / `plan_operations` / `plan_pdfs` subset
of the DD §10 "-- Execution" block (the rest -- `build_boxes`,
`work_sessions`, etc. -- is Phase 3). See DD §4.3 (order ingestion rules),
§5 (WorkOrder/Unit/ExecutionPlan), §6.1/6.2 (order arrival / route
population), §17.4/17.5 (qty/routing-drift reconciliation).

Also lands the FK deferred since migration 0001: `jb2_outbox.work_order_id`
-> `work_orders.id` (that migration's docstring promised it here once
`work_orders` existed).

Ordering within this migration, and why:
  1. `work_orders` (no forward references).
  2. `units` -- `current_plan_op_id` is created as a plain nullable uuid
     column, **no FK yet**, because `plan_operations` doesn't exist until
     step 3. `remake_of_unit_id` is a self-FK, which is fine at create time.
  3. `plan_operations`.
  4. ALTER `units.current_plan_op_id` to add the now-satisfiable FK to
     `plan_operations.id`.
  5. ALTER `jb2_outbox.work_order_id` to add the FK to `work_orders.id`.
  6. `plan_pdfs`.

DEVIATIONS from the DD §10 literal schema sketch:
  - `plan_operations.station_hint` (DD: `fk null`) is omitted -- `stations`
    is a Phase 3 table (migration 0007+); nothing to point the FK at yet.
    Add it there when `stations` lands.
  - `units.serial_number` "uniq-when-set": Postgres unique constraints treat
    NULL as distinct, so a plain `UNIQUE` would already allow multiple NULLs
    -- that's exactly "uniq when set" for Postgres. But a portable-across-
    dialects ordinary unique index would forbid *any* two units from both
    having `serial_number IS NULL`, which is wrong (every unit starts
    null per CR-007). So: a plain (non-unique) btree index everywhere for
    lookups, plus a Postgres-only partial unique index
    `uq_units_serial_number_when_set ON units (serial_number) WHERE
    serial_number IS NOT NULL` for the actual uniqueness rule -- same
    dialect-guarded pattern as `uq_failure_codes_global_code` in migration
    0005 (see that migration's docstring). SQLite (unit/integration tests
    against `app/domain/models_execution.py`) only gets the plain index;
    the model layer must not assume partial uniqueness is DB-enforced there.
  - `units.status` / `remake_of_unit_id` / etc. have no CHECK constraint on
    `status` (unlike `work_orders`/`plan_operations`) -- the DD's literal
    text lists a state machine for units (§5) but the P2-10 task brief only
    specifies "status text" for this table, and Phase 3 (box/session/
    scan/rework machinery) is what actually drives unit status transitions.
    Locking the CHECK down now risks fighting Phase 3's real state list.
    Flagged as a judgment call for Fable's freeze-semantics review.
  - `uq_units_work_order_unit_no` (unit_no unique per work order) and
    `uq_plan_operations_work_order_seq` / `uq_plan_pdfs_work_order_version`
    are additions beyond the DD's literal column list -- straightforward
    per-work-order uniqueness the schema sketch doesn't spell out but the
    domain obviously wants (two units can't both be "unit 3" of the same
    WO). Also flagged for review.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-14

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "work_orders",
        _id_column(),
        sa.Column(
            "jb2_line_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jb2_order_line_items.id"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id"),
            nullable=False,
        ),
        sa.Column("variant_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_sync"),
        sa.Column("plan_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('pending_sync','ready','in_progress','completed',"
            "'blocked_no_instructions','cancelled','cancel_requested','routing_drift')",
            name="ck_work_orders_status",
        ),
        sa.UniqueConstraint(
            "jb2_line_item_id", name="uq_work_orders_jb2_line_item_id"
        ),
    )
    op.create_index("ix_work_orders_product_id", "work_orders", ["product_id"])

    op.create_table(
        "units",
        _id_column(),
        sa.Column(
            "work_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id"),
            nullable=False,
        ),
        sa.Column("unit_no", sa.Integer(), nullable=False),
        sa.Column("serial_number", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("first_pass", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("rework_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "remake_of_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("units.id"),
            nullable=True,
        ),
        # No FK yet -- plan_operations doesn't exist until below. Added via
        # ALTER TABLE further down this function.
        sa.Column("current_plan_op_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "work_order_id", "unit_no", name="uq_units_work_order_unit_no"
        ),
    )
    op.create_index("ix_units_work_order_id", "units", ["work_order_id"])
    op.create_index("ix_units_serial_number", "units", ["serial_number"])
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_units_serial_number_when_set "
            "ON units (serial_number) WHERE serial_number IS NOT NULL"
        )

    op.create_table(
        "plan_operations",
        _id_column(),
        sa.Column(
            "work_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "jb2_routing_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jb2_order_routings.id"),
            nullable=True,
        ),
        sa.Column("operation_code", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "instruction_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruction_sets.id"),
            nullable=True,
        ),
        sa.Column("instruction_version", sa.Integer(), nullable=True),
        sa.Column("frozen_content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("est_minutes", sa.Integer(), nullable=True),
        sa.Column("blocked", sa.Boolean(), nullable=False, server_default="false"),
        sa.CheckConstraint(
            "status in ('pending','active','done','skipped')",
            name="ck_plan_operations_status",
        ),
        sa.UniqueConstraint(
            "work_order_id", "seq", name="uq_plan_operations_work_order_seq"
        ),
    )
    op.create_index("ix_plan_operations_work_order_id", "plan_operations", ["work_order_id"])

    # Deferred FKs, now satisfiable -----------------------------------------
    op.create_foreign_key(
        "fk_units_current_plan_op_id_plan_operations",
        "units",
        "plan_operations",
        ["current_plan_op_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_jb2_outbox_work_order_id_work_orders",
        "jb2_outbox",
        "work_orders",
        ["work_order_id"],
        ["id"],
    )

    op.create_table(
        "plan_pdfs",
        _id_column(),
        sa.Column(
            "work_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # ponytail: plain text, no FK -- operators table arrives Phase 3.
        sa.Column("generated_by", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "work_order_id", "version", name="uq_plan_pdfs_work_order_version"
        ),
    )
    op.create_index("ix_plan_pdfs_work_order_id", "plan_pdfs", ["work_order_id"])


def downgrade() -> None:
    op.drop_table("plan_pdfs")
    op.drop_constraint(
        "fk_jb2_outbox_work_order_id_work_orders", "jb2_outbox", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_units_current_plan_op_id_plan_operations", "units", type_="foreignkey"
    )
    op.drop_table("plan_operations")
    op.drop_table("units")
    op.drop_table("work_orders")
