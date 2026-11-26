"""Import runs and their findings (GRPH-1951 / P92 D3).

Revision ID: 0231
Revises: 0230
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0231"
down_revision: Union[str, None] = "0230"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("commit_sha", sa.String(), nullable=False),
        sa.Column("sources_read", sa.JSON(), nullable=False),
        sa.Column("sources_unread", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_import_runs_project_id", "import_runs", ["project_id"])
    op.create_table(
        "import_findings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("import_runs.id"), nullable=False),
        sa.Column("bucket", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("rule_proposals.id"), nullable=True),
        sa.Column("guidance_id", sa.Integer(), sa.ForeignKey("rule_guidance.id"), nullable=True),
        sa.Column("refused", sa.Text(), nullable=True),
    )
    op.create_index("ix_import_findings_run_id", "import_findings", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_import_findings_run_id", table_name="import_findings")
    op.drop_table("import_findings")
    op.drop_index("ix_import_runs_project_id", table_name="import_runs")
    op.drop_table("import_runs")
