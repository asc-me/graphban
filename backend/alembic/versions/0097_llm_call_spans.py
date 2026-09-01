"""Per-call LLM spans (GRPH-225): the table behind llm_meter.

Hand-written like 0093-0096 — autogenerate sweeps in unrelated drift.

No FK on project_id: a span must outlive the project it was billed to (see the model
docstring). cost_usd and the token columns are nullable on purpose — NULL is the
unpriced/unknown reading, 0 would be a claim about money.

Revision ID: 0097
Revises: 0096
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0097"
down_revision: Union[str, None] = "0096"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_call_spans",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default=""),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(256), nullable=False, server_default=""),
        sa.Column("kind", sa.String(16), nullable=False, server_default="chat"),
        sa.Column("feature", sa.String(64), nullable=False, server_default=""),
        sa.Column("project_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("request_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("cache_read_tokens", sa.Integer, nullable=True),
        sa.Column("cache_write_tokens", sa.Integer, nullable=True),
        sa.Column("tokens_source", sa.String(12), nullable=False, server_default="none"),
        sa.Column("latency_ms", sa.Float, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=True),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("error_class", sa.String(64), nullable=False, server_default=""),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column("retryable", sa.Boolean, nullable=True),
    )
    op.create_index("ix_llm_spans_ts", "llm_call_spans", ["ts"])
    op.create_index("ix_llm_spans_provider", "llm_call_spans", ["provider"])
    op.create_index("ix_llm_spans_project", "llm_call_spans", ["project_id"])
    # The attribution question is always "this project/feature, over time" — one index
    # for the rollup so the analytics cost panel stays a bounded scan.
    op.create_index("ix_llm_spans_feature_ts", "llm_call_spans", ["feature", "ts"])


def downgrade() -> None:
    op.drop_index("ix_llm_spans_feature_ts", table_name="llm_call_spans")
    op.drop_index("ix_llm_spans_project", table_name="llm_call_spans")
    op.drop_index("ix_llm_spans_provider", table_name="llm_call_spans")
    op.drop_index("ix_llm_spans_ts", table_name="llm_call_spans")
    op.drop_table("llm_call_spans")
