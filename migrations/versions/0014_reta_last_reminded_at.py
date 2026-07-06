"""Add health_reminders.last_reminded_at (Bug #1: confirmation window)

Lets a bare "yes"/"done" reply shortly after a reminder count as confirmation.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "health_reminders",
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("health_reminders", "last_reminded_at")
