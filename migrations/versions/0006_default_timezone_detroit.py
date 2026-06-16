"""Backfill placeholder/UTC user timezones to America/Detroit

Early users were created when DEFAULT_TIMEZONE defaulted to "UTC", which made
the bot reason in server time. Move those rows to the real default. Rows whose
timezone was deliberately set to something else are left untouched.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_NEW_DEFAULT = "America/Detroit"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE users SET timezone = :tz "
            "WHERE timezone IS NULL OR timezone = '' OR timezone = 'UTC'"
        ).bindparams(tz=_NEW_DEFAULT)
    )


def downgrade() -> None:
    # Not reversible — we can't tell which rows we changed. No-op.
    pass
