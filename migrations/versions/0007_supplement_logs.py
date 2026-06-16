"""Add supplement_logs table

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supplement_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False, server_default="creatine"),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("taken", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("noon_reminder_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("evening_reminder_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", "date", name="uq_supplement_logs_user_name_date"),
    )
    op.create_index("ix_supplement_logs_user_id", "supplement_logs", ["user_id"])
    op.create_index("ix_supplement_logs_date", "supplement_logs", ["date"])


def downgrade() -> None:
    op.drop_index("ix_supplement_logs_date", table_name="supplement_logs")
    op.drop_index("ix_supplement_logs_user_id", table_name="supplement_logs")
    op.drop_table("supplement_logs")
