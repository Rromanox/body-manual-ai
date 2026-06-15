"""Add message_log table for full chat audit trail

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_log_user_id", "message_log", ["user_id"])
    op.create_index("ix_message_log_telegram_id", "message_log", ["telegram_id"])
    op.create_index("ix_message_log_created_at", "message_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_message_log_created_at", table_name="message_log")
    op.drop_index("ix_message_log_telegram_id", table_name="message_log")
    op.drop_index("ix_message_log_user_id", table_name="message_log")
    op.drop_table("message_log")
