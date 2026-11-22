"""Baseline bench runs and their result records (GRPH-1856, GRPH-P98 S3).

Revision ID: 0228
Revises: 0227
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0228"
down_revision: Union[str, None] = "0227"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bench_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("binary_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("suite_id", sa.String(length=64), nullable=False),
        sa.Column("suite_version", sa.String(length=32), nullable=False),
        sa.Column("tasks", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bench_runs_source_project_id", "bench_runs", ["source_project_id"])
    op.create_index("ix_bench_runs_project_id", "bench_runs", ["project_id"])
    op.create_table(
        "bench_baselines",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("bench_runs.id"), nullable=True),
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("binary_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("suite_id", sa.String(length=64), nullable=False),
        sa.Column("suite_version", sa.String(length=32), nullable=False),
        sa.Column("outcomes", sa.JSON(), nullable=True),
        sa.Column("rollups", sa.JSON(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("wall_seconds", sa.Float(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bench_baselines_source_project_id", "bench_baselines",
                    ["source_project_id"])


def downgrade() -> None:
    op.drop_index("ix_bench_baselines_source_project_id", table_name="bench_baselines")
    op.drop_table("bench_baselines")
    op.drop_index("ix_bench_runs_project_id", table_name="bench_runs")
    op.drop_index("ix_bench_runs_source_project_id", table_name="bench_runs")
    op.drop_table("bench_runs")
