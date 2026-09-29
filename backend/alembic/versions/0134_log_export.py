"""Deployment-wide OTLP log export: one config row for the box, and the batch
rows the status strip's counters are a sum over (PRD-47 S15 / GRPH-966).

`log_export_config` has NO project_id on purpose — the collector is shared, so a
per-project row would be N panels in front of one thing (GRPH-625's argument).

Revision ID: 0134
Revises: 0133
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0134"
down_revision: Union[str, None] = "0133"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "log_export_config",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("endpoint", sa.String(length=512), server_default=""),
        sa.Column("protocol", sa.String(length=24), nullable=False,
                  server_default="http/protobuf"),
        sa.Column("compression", sa.String(length=8), nullable=False, server_default="none"),
        sa.Column("headers", sa.JSON(), nullable=True),
        sa.Column("send_events", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("send_tool_calls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("send_heartbeats", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("event_types", sa.JSON(), nullable=True),
        sa.Column("redact_summaries", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("redact_client_ips", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("mask_api_keys", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cursor_event_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor_call_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "log_export_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False, server_default="export"),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dropped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=32), server_default=""),
        sa.Column("detail", sa.String(length=400), server_default=""),
        sa.Column("endpoint", sa.String(length=512), server_default=""),
    )
    op.create_index("ix_log_export_batches_ts", "log_export_batches", ["ts"])
    op.create_index("ix_log_export_batches_kind_ts", "log_export_batches", ["kind", "ts"])


def downgrade() -> None:
    op.drop_index("ix_log_export_batches_kind_ts", table_name="log_export_batches")
    op.drop_index("ix_log_export_batches_ts", table_name="log_export_batches")
    op.drop_table("log_export_batches")
    op.drop_table("log_export_config")
