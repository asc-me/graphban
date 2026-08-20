"""Project edges and the name registry they resolve against (PRD-21 D3).

Hand-written. `--autogenerate` produced ~200 lines of unrelated drift here — dropping and
recreating every index on `agents`, altering nullability across `api_keys` — because the
models and the chain disagree on index names and NOT NULL in places this change does not
touch. Only the two D3 objects belong in this revision.

Revision ID: 0077
Revises: 0076
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0077"
down_revision: Union[str, None] = "0076"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_edges",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("src_project_id", sa.String(), nullable=False),
        sa.Column("dst_project_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="depends_on"),
        sa.Column("resolved_name", sa.String(), nullable=False, server_default=""),
        # Never empty — an edge that cannot name the file proving it is a guess, and the
        # API refuses one. Enforced in the service rather than as a CHECK so the message
        # can say which entry was missing.
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("fresh", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["src_project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["dst_project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("src_project_id", "dst_project_id", "kind", name="uq_project_edge"),
    )
    op.create_index("ix_project_edges_org_id", "project_edges", ["org_id"])
    op.create_index("ix_project_edges_src_project_id", "project_edges", ["src_project_id"])
    op.create_index("ix_project_edges_dst_project_id", "project_edges", ["dst_project_id"])

    # Existing projects publish nothing until a push says otherwise — an empty registry,
    # which resolves no sibling and is distinct from "we have not looked".
    op.add_column(
        "projects",
        sa.Column("provides", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("projects", "provides")
    op.drop_index("ix_project_edges_dst_project_id", table_name="project_edges")
    op.drop_index("ix_project_edges_src_project_id", table_name="project_edges")
    op.drop_index("ix_project_edges_org_id", table_name="project_edges")
    op.drop_table("project_edges")
