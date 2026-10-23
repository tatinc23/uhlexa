"""initial schema: the 14 Uhlexa tables

Revision ID: 0001
Revises:
Create Date: 2026-10-22 03:52:05.414047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "households",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_households")),
        sa.UniqueConstraint("slug", name=op.f("uq_households_slug")),
    )
    op.create_table(
        "users",
        sa.Column("handle", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.UniqueConstraint("handle", name=op.f("uq_users_handle")),
    )
    op.create_table(
        "webhook_events",
        sa.Column(
            "provider",
            sa.Enum("primitive", "subq", "maritime", name="providername", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("received", "processed", "ignored", "failed", name="webhookstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
        sa.UniqueConstraint("provider", "external_event_id", name="uq_webhook_events_provider_event"),
    )
    op.create_table(
        "agents",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("persona", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column(
            "status", sa.Enum("active", "paused", name="agentstatus", native_enum=False, length=32), nullable=False
        ),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], name=op.f("fk_agents_household_id_households"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_agents_owner_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
    )
    op.create_index(op.f("ix_agents_household_id"), "agents", ["household_id"], unique=False)
    op.create_index(op.f("ix_agents_owner_user_id"), "agents", ["owner_user_id"], unique=False)
    op.create_table(
        "household_memberships",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("guardian", "adult", "child", name="membershiprole", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_household_memberships_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_household_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_household_memberships")),
        sa.UniqueConstraint("household_id", "user_id", name="uq_household_memberships_household_user"),
    )
    op.create_index(op.f("ix_household_memberships_user_id"), "household_memberships", ["user_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued", "running", "succeeded", "failed", "cancelled", name="jobstatus", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], name=op.f("fk_jobs_household_id_households"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"], unique=False)
    op.create_table(
        "conversations",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("topic_key", sa.String(length=120), nullable=False),
        sa.Column(
            "visibility",
            sa.Enum("household", "private", name="visibility", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("open", "archived", name="conversationstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_agent_id", sa.Uuid(), nullable=True),
        sa.Column(
            "transcript_provider",
            sa.Enum("primitive", "local", name="transcriptprovider", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("primitive_thread_id", sa.String(length=255), nullable=True),
        sa.Column("thread_state", sa.JSON(), nullable=False),
        sa.Column("thread_state_version", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_agent_id"],
            ["agents.id"],
            name=op.f("fk_conversations_created_by_agent_id_agents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_conversations_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_conversations_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("primitive_thread_id", name=op.f("uq_conversations_primitive_thread_id")),
    )
    op.create_index(op.f("ix_conversations_household_id"), "conversations", ["household_id"], unique=False)
    op.create_index(op.f("ix_conversations_topic_key"), "conversations", ["topic_key"], unique=False)
    op.create_table(
        "provider_bindings",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column(
            "provider",
            sa.Enum("primitive", "subq", "maritime", name="providername", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("binding_type", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", "error", name="bindingstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_provider_bindings_agent_id_agents"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_provider_bindings_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_provider_bindings_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_bindings")),
        sa.UniqueConstraint("provider", "binding_type", "user_id", name="uq_provider_bindings_provider_type_user"),
    )
    op.create_index(op.f("ix_provider_bindings_household_id"), "provider_bindings", ["household_id"], unique=False)
    op.create_table(
        "conversation_participants",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "participant_type",
            sa.Enum("user", "agent", name="participanttype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "joined_via",
            sa.Enum("created", "resumed", "invited", name="joinedvia", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND agent_id IS NULL) OR (user_id IS NULL AND agent_id IS NOT NULL)",
            name=op.f("ck_conversation_participants_exactly_one_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_conversation_participants_agent_id_agents"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_participants_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_conversation_participants_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_participants")),
        sa.UniqueConstraint("conversation_id", "agent_id", name="uq_conversation_participants_conversation_agent"),
        sa.UniqueConstraint("conversation_id", "user_id", name="uq_conversation_participants_conversation_user"),
    )
    op.create_table(
        "tasks",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_agent_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "general",
                "reminder",
                "chore",
                "shopping_list",
                "purchase",
                "booking",
                "browser",
                name="taskkind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending_approval",
                "approved",
                "running",
                "needs_takeover",
                "completed",
                "failed",
                "denied",
                "cancelled",
                name="taskstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_tasks_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name=op.f("fk_tasks_created_by_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], name=op.f("fk_tasks_household_id_households"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_agent_id"],
            ["agents.id"],
            name=op.f("fk_tasks_requested_by_agent_id_agents"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_index(op.f("ix_tasks_household_id"), "tasks", ["household_id"], unique=False)
    op.create_table(
        "approvals",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_agent_id", sa.Uuid(), nullable=True),
        sa.Column("requested_for_user_id", sa.Uuid(), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "denied", "cancelled", name="approvalstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "decision_source",
            sa.Enum("policy", "policy+snap", name="decisionsource", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
            name=op.f("fk_approvals_decided_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], name=op.f("fk_approvals_household_id_households"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_agent_id"],
            ["agents.id"],
            name=op.f("fk_approvals_requested_by_agent_id_agents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_for_user_id"],
            ["users.id"],
            name=op.f("fk_approvals_requested_for_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_approvals_task_id_tasks"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index(op.f("ix_approvals_household_id"), "approvals", ["household_id"], unique=False)
    op.create_index(op.f("ix_approvals_task_id"), "approvals", ["task_id"], unique=False)
    op.create_table(
        "browser_tasks",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("maritime_computer_id", sa.String(length=255), nullable=True),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "starting",
                "running",
                "needs_takeover",
                "succeeded",
                "failed",
                "cancelled",
                "unavailable",
                name="browsertaskstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("last_screenshot_ref", sa.String(length=512), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("requires_takeover", sa.Boolean(), nullable=False),
        sa.Column("takeover_url", sa.Text(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_browser_tasks_agent_id_agents"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_browser_tasks_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_browser_tasks_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_browser_tasks_task_id_tasks"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_browser_tasks_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_tasks")),
    )
    op.create_index(op.f("ix_browser_tasks_household_id"), "browser_tasks", ["household_id"], unique=False)
    op.create_index(op.f("ix_browser_tasks_task_id"), "browser_tasks", ["task_id"], unique=False)
    op.create_table(
        "messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "sender_type",
            sa.Enum("user", "agent", "system", name="sendertype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("sender_user_id", sa.Uuid(), nullable=True),
        sa.Column("sender_agent_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("primitive_message_id", sa.String(length=255), nullable=True),
        sa.Column(
            "source",
            sa.Enum("primitive", "local", name="transcriptprovider", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_agent_id"], ["agents.id"], name=op.f("fk_messages_sender_agent_id_agents"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["sender_user_id"], ["users.id"], name=op.f("fk_messages_sender_user_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_messages_task_id_tasks"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("primitive_message_id", name=op.f("uq_messages_primitive_message_id")),
    )
    op.create_index(
        "ix_messages_conversation_id_created_at", "messages", ["conversation_id", "created_at"], unique=False
    )
    op.create_table(
        "activity_events",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "actor_type",
            sa.Enum("user", "agent", "system", "provider", name="actortype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_agent_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("browser_task_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_agent_id"],
            ["agents.id"],
            name=op.f("fk_activity_events_actor_agent_id_agents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name=op.f("fk_activity_events_actor_user_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["browser_task_id"],
            ["browser_tasks.id"],
            name=op.f("fk_activity_events_browser_task_id_browser_tasks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_activity_events_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_activity_events_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_activity_events_task_id_tasks"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_events")),
    )
    op.create_index(
        "ix_activity_events_household_id_created_at", "activity_events", ["household_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_table("activity_events")
    op.drop_table("messages")
    op.drop_table("browser_tasks")
    op.drop_table("approvals")
    op.drop_table("tasks")
    op.drop_table("conversation_participants")
    op.drop_table("provider_bindings")
    op.drop_table("conversations")
    op.drop_table("jobs")
    op.drop_table("household_memberships")
    op.drop_table("agents")
    op.drop_table("webhook_events")
    op.drop_table("users")
    op.drop_table("households")
