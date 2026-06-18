"""Add goal_weight_lbs to users

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("goal_weight_lbs", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "goal_weight_lbs")
