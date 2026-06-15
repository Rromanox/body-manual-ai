"""Add sleep detail columns: bedtime, wake time, REM, deep, light sleep hours

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-15

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("daily_metrics", sa.Column("sleep_start_local", sa.String(8), nullable=True))
    op.add_column("daily_metrics", sa.Column("sleep_end_local", sa.String(8), nullable=True))
    op.add_column("daily_metrics", sa.Column("rem_sleep_hours", sa.Float(), nullable=True))
    op.add_column("daily_metrics", sa.Column("deep_sleep_hours", sa.Float(), nullable=True))
    op.add_column("daily_metrics", sa.Column("light_sleep_hours", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("daily_metrics", "light_sleep_hours")
    op.drop_column("daily_metrics", "deep_sleep_hours")
    op.drop_column("daily_metrics", "rem_sleep_hours")
    op.drop_column("daily_metrics", "sleep_end_local")
    op.drop_column("daily_metrics", "sleep_start_local")
