"""outbox retry scheduling: next_attempt_at

P1-09: the drainer needs a persisted "don't retry before this time" column
for exponential backoff that survives process restarts. Nullable — NULL
means eligible to attempt now.

Revision numbering note: 0002 (sync_checkpoints, a parallel Phase 1 task)
did not exist in migrations/versions/ when this file was started; it
appeared before this task finished, so per the coordination instructions
this migration is re-chained onto it: 0001 -> 0002 -> 0003.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-14

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jb2_outbox",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jb2_outbox", "next_attempt_at")
