"""display cache (P1-13, CR-011)

`display_cache`: last-good payload for the two unfiltered/heavy JB2 display
feeds (`shopview/get-jobs`, `eci-aps/get-schedule`). One row per feed,
keyed by sync-resource name; see app/sync/display.py.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-14

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "display_cache",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("display_cache")
