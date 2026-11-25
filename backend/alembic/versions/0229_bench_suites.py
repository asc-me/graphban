"""Server repo-suite store: a version counter and task rows per project (GRPH-1857).

Revision ID: 0229
Revises: 0228
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0229"
down_revision: Union[str, None] = "0228"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bench_suites",
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "bench_suite_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("acceptance", sa.JSON(), nullable=True),
        sa.Column("touchpoints", sa.JSON(), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("size_band", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("check", sa.JSON(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=False, server_default=""),
        sa.Column("expect", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("ref", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "task_id", name="uq_bench_suite_task"),
    )
    op.create_index("ix_bench_suite_tasks_project_id", "bench_suite_tasks", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_bench_suite_tasks_project_id", table_name="bench_suite_tasks")
    op.drop_table("bench_suite_tasks")
    op.drop_table("bench_suites")
