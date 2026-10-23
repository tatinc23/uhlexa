"""trusted_contacts: opt-in links between adults in different households

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23 07:00:00.000000

Ken's decision (2026-10-22): cross-household handoffs and contact are allowed only through a link both adults accept.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trusted_contacts",
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("requester_household_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_household_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("offered", "active", "revoked", name="trustedcontactstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["requester_household_id"],
            ["households.id"],
            name=op.f("fk_trusted_contacts_requester_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requester_user_id"],
            ["users.id"],
            name=op.f("fk_trusted_contacts_requester_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["users.id"],
            name=op.f("fk_trusted_contacts_revoked_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_household_id"],
            ["households.id"],
            name=op.f("fk_trusted_contacts_target_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["users.id"], name=op.f("fk_trusted_contacts_target_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trusted_contacts")),
    )
    op.create_index(op.f("ix_trusted_contacts_requester_user_id"), "trusted_contacts", ["requester_user_id"])
    op.create_index(op.f("ix_trusted_contacts_target_user_id"), "trusted_contacts", ["target_user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_trusted_contacts_target_user_id"), table_name="trusted_contacts")
    op.drop_index(op.f("ix_trusted_contacts_requester_user_id"), table_name="trusted_contacts")
    op.drop_table("trusted_contacts")
