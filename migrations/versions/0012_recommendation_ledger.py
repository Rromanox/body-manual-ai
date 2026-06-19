"""Add recommendation_ledger table (Recommendation Ledger Phase 3A)

Foundation only: creates the table. No data, no extraction, no checkpoint
evaluation, no wiring into any flow yet.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recommendation_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("recommendation_type", sa.String(24), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("recommendation_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("trigger_data", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("expected_outcome", sa.Text(), nullable=True),
        sa.Column("checkpoint_metric", sa.String(32), nullable=True),
        sa.Column("checkpoint_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("followed_status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("outcome_status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("outcome_summary", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(12), nullable=False, server_default="medium"),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_message_id"], ["coach_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recommendation_ledger_user_id", "recommendation_ledger", ["user_id"])
    op.create_index("ix_recommendation_ledger_local_date", "recommendation_ledger", ["local_date"])
    op.create_index("ix_recommendation_ledger_source_message_id", "recommendation_ledger", ["source_message_id"])
    op.create_index("ix_recommendation_ledger_user_date", "recommendation_ledger", ["user_id", "local_date"])
    op.create_index("ix_recommendation_ledger_user_status", "recommendation_ledger", ["user_id", "status"])
    op.create_index("ix_recommendation_ledger_user_checkpoint", "recommendation_ledger", ["user_id", "checkpoint_date"])


def downgrade() -> None:
    op.drop_index("ix_recommendation_ledger_user_checkpoint", table_name="recommendation_ledger")
    op.drop_index("ix_recommendation_ledger_user_status", table_name="recommendation_ledger")
    op.drop_index("ix_recommendation_ledger_user_date", table_name="recommendation_ledger")
    op.drop_index("ix_recommendation_ledger_source_message_id", table_name="recommendation_ledger")
    op.drop_index("ix_recommendation_ledger_local_date", table_name="recommendation_ledger")
    op.drop_index("ix_recommendation_ledger_user_id", table_name="recommendation_ledger")
    op.drop_table("recommendation_ledger")
