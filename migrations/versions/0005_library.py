"""library schema (P2-01)

Creates the "Library" block of DD §10: `products`, `product_part_map`,
`instruction_sets`, `steps`, `substeps`, `failure_codes`. See DD §5 (core
domain model / substep types), §7 (instruction library builder / versioning
rules).

DEVIATION from DD §10 literal text: the DD's inline schema sketch says
`instruction_sets` uniqueness is (product_id, operation_match, version).
`operation_match` is jsonb — Postgres unique constraints/indexes can only
reference jsonb columns via an expression (e.g. hashing it or extracting
scalar fields), not the column directly, so a straight
`UNIQUE (product_id, operation_match, version)` is not portable/reliable.
This migration instead enforces `UNIQUE (product_id, title, version)`
(`uq_instruction_sets_product_title_version`). `title` is the human-readable
slot the builder UI already requires to be stable per (product, operation),
so it's a reasonable proxy scope key. Flagged for F's review per the
IMPLEMENTATION_PLAN P2-01 row ("S (**F reviews**)").

`failure_codes` uniqueness: DD says "code uniq-per-scope". Modeled as
`UNIQUE (product_id, code)` (`uq_failure_codes_product_code`) for the normal
per-product-scope case. Postgres treats NULL as distinct in unique
constraints, so that constraint alone would let multiple *global*
(`product_id IS NULL`) rows share the same `code` — not what "uniq-per-scope"
means for the global scope. A partial unique index
(`uq_failure_codes_global_code`, `ON failure_codes (code) WHERE product_id
IS NULL`) closes that gap. Partial indexes are a Postgres-only feature, so
the `CREATE UNIQUE INDEX ... WHERE ...` is emitted via `op.execute` guarded
on `op.get_context().dialect.name == "postgresql"`; this repo's migrations
only ever run against Postgres (see migrations/env.py — no sqlite branch),
so the guard is inert here but documents the portability boundary for
anyone later pointing alembic at sqlite. Model-level tests that exercise
`app/domain/models_library.py` against sqlite (if any are added) will not
see this partial-uniqueness rule enforced by the DB — that's a known gap,
not a bug, and the model layer should not assume it.

`created_by` / `published_by` on `instruction_sets` are plain text (operator
display names) for now, not FKs — the `operators` table doesn't land until
Phase 3 (JB2 employee sync + badge auth, see plan). Revisit once it exists.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-14

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
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
        "products",
        _id_column(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("variant_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.UniqueConstraint("name", name="uq_products_name"),
    )

    op.create_table(
        "product_part_map",
        _id_column(),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id"),
            nullable=False,
        ),
        sa.Column("jb2_part_number", sa.Text(), nullable=False),
        sa.Column("variant_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.UniqueConstraint("jb2_part_number", name="uq_product_part_map_jb2_part_number"),
    )
    op.create_index(
        "ix_product_part_map_product_id", "product_part_map", ["product_id"]
    )

    op.create_table(
        "instruction_sets",
        _id_column(),
        # null = global scope (DD §7.1: shared sets defined once, attached
        # to many products; product-specific sets override globals).
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id"),
            nullable=True,
        ),
        sa.Column("operation_match", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "parent_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruction_sets.id"),
            nullable=True,
        ),
        sa.Column("est_minutes", sa.Integer(), nullable=True),
        # ponytail: plain text, no FK -- operators table arrives Phase 3
        # (JB2 employee sync + badge auth). Revisit then.
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("published_by", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state in ('draft','in_review','published','retired')",
            name="ck_instruction_sets_state",
        ),
        # DEVIATION from DD §10 literal (product_id, operation_match,
        # version): operation_match is jsonb and can't portably sit in a
        # unique constraint. See migration docstring.
        sa.UniqueConstraint(
            "product_id", "title", "version", name="uq_instruction_sets_product_title_version"
        ),
    )
    op.create_index(
        "ix_instruction_sets_product_id", "instruction_sets", ["product_id"]
    )
    op.create_index(
        "ix_instruction_sets_parent_version_id", "instruction_sets", ["parent_version_id"]
    )

    op.create_table(
        "steps",
        _id_column(),
        sa.Column(
            "instruction_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruction_sets.id"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("est_minutes", sa.Integer(), nullable=True),
        sa.UniqueConstraint("instruction_set_id", "seq", name="uq_steps_instruction_set_seq"),
    )
    op.create_index("ix_steps_instruction_set_id", "steps", ["instruction_set_id"])

    op.create_table(
        "substeps",
        _id_column(),
        sa.Column(
            "step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("steps.id"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("condition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "measurement_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "media",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("signoff_role", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "type in ('action','measurement','inspection','photo','material','signoff')",
            name="ck_substeps_type",
        ),
        sa.UniqueConstraint("step_id", "seq", name="uq_substeps_step_seq"),
    )
    op.create_index("ix_substeps_step_id", "substeps", ["step_id"])

    op.create_table(
        "failure_codes",
        _id_column(),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id"),
            nullable=True,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("jb2_reason_number", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.UniqueConstraint("product_id", "code", name="uq_failure_codes_product_code"),
    )
    op.create_index("ix_failure_codes_product_id", "failure_codes", ["product_id"])

    # Partial unique index: closes the "NULLs are distinct" gap so global
    # (product_id IS NULL) failure codes are still unique per DD's
    # "code uniq-per-scope". Postgres-only feature (dialect-guarded); see
    # migration docstring for the sqlite caveat.
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_failure_codes_global_code "
            "ON failure_codes (code) WHERE product_id IS NULL"
        )


def downgrade() -> None:
    op.drop_table("failure_codes")
    op.drop_table("substeps")
    op.drop_table("steps")
    op.drop_table("instruction_sets")
    op.drop_table("product_part_map")
    op.drop_table("products")
