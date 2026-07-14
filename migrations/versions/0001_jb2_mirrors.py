"""jb2 mirror layer baseline

DEFERRED-VERIFY: run against live Postgres 16 when available (plan §9).

Creates the JB2 mirror tables (DD §10 "-- JB2 mirrors" block) plus the
sync/outbox/mapping-exception support tables. This is the Phase 1 baseline;
nothing from the "Library"/"Execution" blocks of §10 exists yet.

`jb2_outbox.work_order_id` is a **plain nullable uuid column, no FK** — the
`work_orders` table doesn't exist until Phase 2. Migration 0003 (Phase 2)
adds the FK constraint once `work_orders` lands. See DD §10 and §4.5.

Revision ID: 0001
Revises:
Create Date: 2026-07-14

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _mirror_columns() -> list[sa.Column]:
    """Columns every jb2_* mirror table gets per DD §4.2: raw payload,
    content hash for change detection, JB2's own last-modified stamp, and
    when the MES last synced the row."""
    return [
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("jb2_last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
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
        "jb2_orders",
        _id_column(),
        sa.Column("jb2_id", sa.Text(), nullable=False),
        sa.Column("order_number", sa.Text(), nullable=True),
        sa.Column("customer", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("jb2_id", name="uq_jb2_orders_jb2_id"),
    )

    op.create_table(
        "jb2_order_line_items",
        _id_column(),
        sa.Column("jb2_id", sa.Text(), nullable=False),
        sa.Column(
            "jb2_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jb2_orders.id"),
            nullable=True,
        ),
        sa.Column("part_number", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("qty", sa.Numeric(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("jb2_id", name="uq_jb2_order_line_items_jb2_id"),
    )
    op.create_index(
        "ix_jb2_order_line_items_jb2_order_id",
        "jb2_order_line_items",
        ["jb2_order_id"],
    )

    op.create_table(
        "jb2_order_routings",
        _id_column(),
        sa.Column("jb2_id", sa.Text(), nullable=False),
        sa.Column(
            "jb2_line_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jb2_order_line_items.id"),
            nullable=True,
        ),
        sa.Column("seq", sa.Integer(), nullable=True),
        sa.Column("operation_code", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("work_center_code", sa.Text(), nullable=True),
        sa.Column("est_setup_hrs", sa.Numeric(), nullable=True),
        sa.Column("est_run_hrs", sa.Numeric(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("jb2_id", name="uq_jb2_order_routings_jb2_id"),
    )
    op.create_index(
        "ix_jb2_order_routings_jb2_line_item_id",
        "jb2_order_routings",
        ["jb2_line_item_id"],
    )

    op.create_table(
        "jb2_order_materials",
        _id_column(),
        sa.Column("jb2_id", sa.Text(), nullable=False),
        sa.Column(
            "jb2_line_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jb2_order_line_items.id"),
            nullable=True,
        ),
        sa.Column("routing_seq", sa.Integer(), nullable=True),
        sa.Column("part_number", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("qty_planned", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("unit_cost", sa.Numeric(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("jb2_id", name="uq_jb2_order_materials_jb2_id"),
    )
    op.create_index(
        "ix_jb2_order_materials_jb2_line_item_id",
        "jb2_order_materials",
        ["jb2_line_item_id"],
    )

    op.create_table(
        "jb2_parts",
        _id_column(),
        sa.Column("jb2_id", sa.Text(), nullable=False),
        sa.Column("part_number", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("revision", sa.Text(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("jb2_id", name="uq_jb2_parts_jb2_id"),
    )

    op.create_table(
        "jb2_work_centers",
        _id_column(),
        sa.Column("jb2_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("jb2_id", name="uq_jb2_work_centers_jb2_id"),
    )

    op.create_table(
        "jb2_employees",
        _id_column(),
        sa.Column("jb2_id", sa.Text(), nullable=False),
        sa.Column("employee_code", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("jb2_id", name="uq_jb2_employees_jb2_id"),
    )

    op.create_table(
        "jb2_operation_codes",
        _id_column(),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("work_center_code", sa.Text(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("code", name="uq_jb2_operation_codes_code"),
    )

    op.create_table(
        "jb2_reason_codes",
        _id_column(),
        sa.Column("reason_number", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("reason_number", name="uq_jb2_reason_codes_reason_number"),
    )

    op.create_table(
        "jb2_documents",
        _id_column(),
        sa.Column("jb2_id", sa.Text(), nullable=False),
        sa.Column("document_number", sa.Text(), nullable=True),
        sa.Column("revision", sa.Text(), nullable=True),
        sa.Column("linked_part_number", sa.Text(), nullable=True),
        *_mirror_columns(),
        sa.UniqueConstraint("jb2_id", name="uq_jb2_documents_jb2_id"),
    )

    op.create_table(
        "sync_runs",
        _id_column(),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_sync_runs_resource_started_at", "sync_runs", ["resource", "started_at"])

    op.create_table(
        "jb2_outbox",
        _id_column(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        # ponytail: no FK yet. work_orders lands in Phase 2; migration 0003
        # adds `sa.ForeignKey("work_orders.id")` once that table exists.
        sa.Column(
            "work_order_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Future FK to work_orders.id — table doesn't exist until Phase 2 "
            "(migration 0003 adds the constraint).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_jb2_outbox_idempotency_key"),
        sa.CheckConstraint(
            "status in ('pending','sent','confirmed','failed')",
            name="ck_jb2_outbox_status",
        ),
    )
    op.create_index("ix_jb2_outbox_status_created_at", "jb2_outbox", ["status", "created_at"])
    op.create_index("ix_jb2_outbox_work_order_id", "jb2_outbox", ["work_order_id"])

    op.create_table(
        "mapping_exceptions",
        _id_column(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_mapping_exceptions_kind_resolved", "mapping_exceptions", ["kind", "resolved"]
    )


def downgrade() -> None:
    op.drop_table("mapping_exceptions")
    op.drop_table("jb2_outbox")
    op.drop_table("sync_runs")
    op.drop_table("jb2_documents")
    op.drop_table("jb2_reason_codes")
    op.drop_table("jb2_operation_codes")
    op.drop_table("jb2_employees")
    op.drop_table("jb2_work_centers")
    op.drop_table("jb2_parts")
    op.drop_table("jb2_order_materials")
    op.drop_table("jb2_order_routings")
    op.drop_table("jb2_order_line_items")
    op.drop_table("jb2_orders")
