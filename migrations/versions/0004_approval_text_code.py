"""approval_text_code: one-time hashed code for APPROVE/DENY by text

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23 09:00:00.000000

The old code was the first 8 hex characters of the approval id (guessable by anyone who could see the id).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("text_code_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("text_code_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_column("text_code_expires_at")
        batch.drop_column("text_code_hash")
