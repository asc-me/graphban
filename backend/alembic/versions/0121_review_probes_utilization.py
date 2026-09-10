"""PRD-41 S3: review checks, probe runs, utilization on rollups.

`harness_review_checks` is a row per reviewed attempt so a withdrawal can recompute
(D6). `capability_probe_runs` is the operator-started panel (D7/D8). Rollups gain
`turns_used` / `turns_reported` / `budget_hits` (§7.3).

Revision ID: 0121
Revises: 0120
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0121"
down_revision: Union[str, None] = "0120"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("harness_rollups", "platform_rollups"):
        op.add_column(table, sa.Column("turns_used", sa.Integer(),
                                       nullable=False, server_default="0"))
        op.add_column(table, sa.Column("turns_reported", sa.Integer(),
                                       nullable=False, server_default="0"))
        op.add_column(table, sa.Column("budget_hits", sa.Integer(),
                                       nullable=False, server_default="0"))

    op.create_table(
        "harness_review_checks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("delegation_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column("reviewer_agent_id", sa.String(), nullable=False),
        sa.Column("reviewer_vendor", sa.String(32), nullable=True),
        sa.Column("reviewer_model", sa.String(64), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("unconfirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("size_band", sa.String(1), nullable=True),
        sa.Column("bounce_category", sa.String(16), nullable=True),
        sa.Column("head_commit", sa.String(), nullable=True),
        sa.Column("contradicted_by", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_harness_review_checks_verdict", "harness_review_checks",
                    ["delegation_id", "reviewer_agent_id"], unique=True)
    op.create_index("ix_harness_review_checks_project", "harness_review_checks",
                    ["project_id"])

    op.create_table(
        "capability_probe_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=False),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("vendor", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("binary_version", sa.String(32), nullable=False, server_default=""),
        sa.Column("capability", sa.String(8), nullable=False),
        sa.Column("item_ids", sa.JSON(), nullable=True),
        sa.Column("estimated_tokens", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
    )
    op.create_index("ix_capability_probe_runs_source", "capability_probe_runs",
                    ["source_project_id"])
    op.create_index("ix_capability_probe_runs_project", "capability_probe_runs",
                    ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_capability_probe_runs_project", table_name="capability_probe_runs")
    op.drop_index("ix_capability_probe_runs_source", table_name="capability_probe_runs")
    op.drop_table("capability_probe_runs")
    op.drop_index("ix_harness_review_checks_project", table_name="harness_review_checks")
    op.drop_index("ix_harness_review_checks_verdict", table_name="harness_review_checks")
    op.drop_table("harness_review_checks")
    for table in ("platform_rollups", "harness_rollups"):
        op.drop_column(table, "budget_hits")
        op.drop_column(table, "turns_reported")
        op.drop_column(table, "turns_used")
