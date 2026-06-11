"""initial schema — all six tables (SPEC §Database Schema, from day one)

Revision ID: 0001
Revises:
Create Date: 2026-06-10

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("goal", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_oauth_connections_user_provider"),
    )
    op.create_index("ix_oauth_connections_user_id", "oauth_connections", ["user_id"])

    op.create_table(
        "daily_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("recovery_score", sa.Float(), nullable=True),
        sa.Column("hrv_ms", sa.Float(), nullable=True),
        sa.Column("resting_heart_rate", sa.Float(), nullable=True),
        sa.Column("respiratory_rate", sa.Float(), nullable=True),
        sa.Column("spo2", sa.Float(), nullable=True),
        sa.Column("skin_temp", sa.Float(), nullable=True),
        sa.Column("sleep_hours", sa.Float(), nullable=True),
        sa.Column("sleep_efficiency", sa.Float(), nullable=True),
        sa.Column("sleep_performance", sa.Float(), nullable=True),
        sa.Column("sleep_consistency", sa.Float(), nullable=True),
        sa.Column("strain", sa.Float(), nullable=True),
        sa.Column("workout_count", sa.Integer(), nullable=True),
        sa.Column("total_workout_minutes", sa.Float(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("body_fat_pct", sa.Float(), nullable=True),
        sa.Column("muscle_mass", sa.Float(), nullable=True),
        sa.Column("fat_free_mass", sa.Float(), nullable=True),
        sa.Column("water_pct", sa.Float(), nullable=True),
        sa.Column("bone_mass", sa.Float(), nullable=True),
        sa.Column("bmi", sa.Float(), nullable=True),
        sa.Column("raw_whoop_json", postgresql.JSONB(), nullable=True),
        sa.Column("raw_withings_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "date", name="uq_daily_metrics_user_date"),
    )
    op.create_index("ix_daily_metrics_user_id", "daily_metrics", ["user_id"])
    op.create_index("ix_daily_metrics_date", "daily_metrics", ["date"])

    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=False),
        sa.Column("feel_score", sa.Integer(), nullable=True),
        sa.Column("free_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_journal_entries_user_id", "journal_entries", ["user_id"])
    op.create_index("ix_journal_entries_date", "journal_entries", ["date"])

    op.create_table(
        "coach_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("message_type", sa.String(length=16), nullable=False),
        sa.Column("summary_payload", postgresql.JSONB(), nullable=True),
        sa.Column("ai_response", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_coach_messages_user_id", "coach_messages", ["user_id"])
    op.create_index("ix_coach_messages_date", "coach_messages", ["date"])

    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pattern_key", sa.String(length=64), nullable=False),
        sa.Column("pattern_description", sa.Text(), nullable=False),
        sa.Column("trigger_tag", sa.String(length=32), nullable=True),
        sa.Column("outcome_metric", sa.String(length=32), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("supporting_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("opposing_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=True),
        sa.Column("last_seen", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="watching", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "pattern_key", name="uq_observations_user_pattern"),
    )
    op.create_index("ix_observations_user_id", "observations", ["user_id"])


def downgrade() -> None:
    op.drop_table("observations")
    op.drop_table("coach_messages")
    op.drop_table("journal_entries")
    op.drop_table("daily_metrics")
    op.drop_table("oauth_connections")
    op.drop_table("users")
