"""Add max_heart_rate and height_meter to users (from WHOOP read:body_measurement scope)

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-15

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("max_heart_rate", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("height_meter", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "height_meter")
    op.drop_column("users", "max_heart_rate")
