"""sync checkpoints (P1-05)

Per-resource incremental-sync checkpoint: the last successfully processed
`lastModDate` for a resource (DD §4.2, docs/jb2-api-findings.md §4). One row
per resource name, upserted by app/sync/checkpoints.py.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-14

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_checkpoints",
        sa.Column("resource", sa.Text(), primary_key=True),
        sa.Column("checkpoint", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("sync_checkpoints")
