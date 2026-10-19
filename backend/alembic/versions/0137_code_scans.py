"""Code health scans: CodeScan + ScanFinding (GRPH-1035 / PRD-P51 §3).

Agent-reported scanner results keyed by path + commit sha. A missing row is
never-scanned; a row with zero findings is clean only at that sha. Absence must
not read as clean — the same honesty rule as the rest of Stack.

Revision ID: 0137
Revises: 0136
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0137"
down_revision: Union[str, None] = "0136"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "code_scans",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), server_default="security", nullable=False),
        sa.Column("scanner_id", sa.String(), nullable=False),
        sa.Column("scanner_version", sa.String(), server_default="", nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("scanned_sha", sa.String(), server_default="", nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_by", sa.String(), server_default="", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_code_scans_project_scanner_path",
        "code_scans",
        ["project_id", "scanner_id", "path"],
    )
    op.create_index(
        "ix_code_scans_project_category_path",
        "code_scans",
        ["project_id", "category", "path"],
    )

    op.create_table(
        "scan_findings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scan_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), server_default="", nullable=False),
        sa.Column("severity", sa.String(), server_default="medium", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("link_kind", sa.String(), server_default="", nullable=False),
        sa.Column("link_ref", sa.String(), server_default="", nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["scan_id"], ["code_scans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scan_findings_scan_id", "scan_findings", ["scan_id"])
    op.create_index("ix_scan_findings_project_id", "scan_findings", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_scan_findings_project_id", table_name="scan_findings")
    op.drop_index("ix_scan_findings_scan_id", table_name="scan_findings")
    op.drop_table("scan_findings")
    op.drop_index("ix_code_scans_project_category_path", table_name="code_scans")
    op.drop_index("ix_code_scans_project_scanner_path", table_name="code_scans")
    op.drop_table("code_scans")
