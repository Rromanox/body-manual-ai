"""Add training_sessions and training_log tables (12-week training plan module)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("session_type", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("duration_min", sa.Integer(), nullable=True),
        sa.Column("loaded", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("priority", sa.String(8), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("moved_from", sa.Date(), nullable=True),
        sa.Column("completed_notes", sa.Text(), nullable=True),
        sa.Column("recovery_adjustment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "date", name="uq_training_sessions_user_date"),
    )
    op.create_index("ix_training_sessions_user_id", "training_sessions", ["user_id"])
    op.create_index("ix_training_sessions_date", "training_sessions", ["date"])

    op.create_table(
        "training_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_training_log_user_id", "training_log", ["user_id"])
    op.create_index("ix_training_log_session_date", "training_log", ["session_date"])


def downgrade() -> None:
    op.drop_index("ix_training_log_session_date", table_name="training_log")
    op.drop_index("ix_training_log_user_id", table_name="training_log")
    op.drop_table("training_log")
    op.drop_index("ix_training_sessions_date", table_name="training_sessions")
    op.drop_index("ix_training_sessions_user_id", table_name="training_sessions")
    op.drop_table("training_sessions")
