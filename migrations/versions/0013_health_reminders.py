"""Add health_reminders table (recurring interval reminders, e.g. retatrutide)

User-specified recurring reminders only — not medical advice, no dosage.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "health_reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reminder_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("interval_days", sa.Integer(), nullable=False),
        sa.Column("last_completed_date", sa.Date(), nullable=True),
        sa.Column("next_due_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_reminded_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "reminder_type", name="uq_health_reminders_user_type"),
    )
    op.create_index("ix_health_reminders_user_id", "health_reminders", ["user_id"])
    op.create_index("ix_health_reminders_next_due_date", "health_reminders", ["next_due_date"])


def downgrade() -> None:
    op.drop_index("ix_health_reminders_next_due_date", table_name="health_reminders")
    op.drop_index("ix_health_reminders_user_id", table_name="health_reminders")
    op.drop_table("health_reminders")
