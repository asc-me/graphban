"""The observed feed: every MCP call, attributed to the agent (PRD-34 D2).

Live (PRD-33) shows who is here. It could not show what an agent is doing because
the dispatcher wrote down mutations only, attributed to the credential; twenty-one
read tools — most of what a working agent calls — left no trace. This table is
that trace: one row per call, success or refusal, with the agent when it can be
named and NULL when it cannot (counted, never guessed).

Not `events`. That is the audit ledger, kept forever, one row per accepted
mutation. This is telemetry with retention, so it gets its own table rather than
changing what Activity means.

Revision ID: 0102
Revises: 0101
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0102"
down_revision: Union[str, None] = "0101"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("api_key_id", sa.String(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="observed"),
        sa.Column("tool", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("target", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=200), nullable=True),
        sa.Column("files", sa.JSON(), nullable=True),
    )
    op.create_index("ix_agent_calls_ts", "agent_calls", ["ts"])
    op.create_index("ix_agent_calls_project_id", "agent_calls", ["project_id"])
    op.create_index("ix_agent_calls_agent_id", "agent_calls", ["agent_id"])
    op.create_index("ix_agent_calls_api_key_id", "agent_calls", ["api_key_id"])
    # The feed read and the board summary (D19) walk (agent, ts); the sweep walks
    # (project, ts). Both composite so neither degrades into a table scan on a busy box.
    op.create_index("ix_agent_calls_agent_ts", "agent_calls", ["agent_id", "ts"])
    op.create_index("ix_agent_calls_project_ts", "agent_calls", ["project_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_agent_calls_project_ts", table_name="agent_calls")
    op.drop_index("ix_agent_calls_agent_ts", table_name="agent_calls")
    op.drop_index("ix_agent_calls_api_key_id", table_name="agent_calls")
    op.drop_index("ix_agent_calls_agent_id", table_name="agent_calls")
    op.drop_index("ix_agent_calls_project_id", table_name="agent_calls")
    op.drop_index("ix_agent_calls_ts", table_name="agent_calls")
    op.drop_table("agent_calls")
