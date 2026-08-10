"""What a promoted lesson should become (GRPH-307 / PRD-16).

Tiers: fact / rule / hook / skill / agent / allowlist / update / delete.

Keyed on (tier, scope) rather than on the lesson, because PRD-16's acceptance criterion is
that two lessons landing on the same tier and scope produce ONE recommendation — the second
superseding the first, not two competing creates that would install two files doing the
same job and leave a reviewer to reconcile them.

`supersedes_id` keeps the earlier row rather than overwriting it. "This grew from three
lessons over two weeks" is exactly the thing you cannot reconstruct after the fact, and it
is the same append-only reasoning the baseline chain rests on.

`status` starts `queued`. A reviewed row is never flipped back by a later run — which is
why classification only considers lessons that have never had a recommendation.

Revision ID: 0059
Revises: 0058
"""
from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_recommendations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("lesson_ids", sa.JSON(), nullable=True),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.Column("graded_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_artifact_recommendations_status", "artifact_recommendations",
                    ["status"])


def downgrade() -> None:
    op.drop_index("ix_artifact_recommendations_status",
                  table_name="artifact_recommendations")
    op.drop_table("artifact_recommendations")
