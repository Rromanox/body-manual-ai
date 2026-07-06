"""Add job_runs table (Fix #2: scheduled-job execution audit log)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_runs_job_name", "job_runs", ["job_name"])
    op.create_index("ix_job_runs_created_at", "job_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_job_runs_created_at", table_name="job_runs")
    op.drop_index("ix_job_runs_job_name", table_name="job_runs")
    op.drop_table("job_runs")
