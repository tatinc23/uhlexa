"""channels: one owner per external binding, and a handoff assignee on tasks

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22 18:00:00.000000

Addendum 21.7. `ProviderName` gains KEIKI/INKBOX and `TaskKind` gains HANDOFF; those are VARCHAR(32) columns with no
CHECK constraint, so they need no DDL. Batch mode keeps the ALTERs portable to SQLite.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_provider_bindings_provider_type_external",
        "provider_bindings",
        ["provider", "binding_type", "external_id"],
        unique=True,
    )
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("assignee_user_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_tasks_assignee_user_id_users"), "users", ["assignee_user_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index(op.f("ix_tasks_assignee_user_id"), ["assignee_user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index(op.f("ix_tasks_assignee_user_id"))
        batch.drop_constraint(op.f("fk_tasks_assignee_user_id_users"), type_="foreignkey")
        batch.drop_column("assignee_user_id")
    op.drop_index("uq_provider_bindings_provider_type_external", table_name="provider_bindings")
