"""Reach, lesson class, attribution, and the outcomes log.

Hand-written for the reason 0077-0080 and 0093 were: `--autogenerate` sweeps in unrelated
drift (and gets EmbeddingType/pgvector columns wrong).

`reach` defaults to project so existing rows and old imports are not silently org-wide.
Attribution columns are nullable with no backfill: NULL is unmeasured, and filling them
from `origin` or `project_id` would make eligibility fire on junk. ON DELETE SET NULL so a
deleted user or project does not remain a countable ghost.

Revision ID: 0094
Revises: 0093
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0094"
down_revision: Union[str, None] = "0093"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_shards",
        sa.Column("reach", sa.String(), nullable=False, server_default="project"),
    )
    op.add_column(
        "memory_shards",
        sa.Column("lesson_class", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "memory_shards",
        sa.Column("actor_user_id", sa.String(), nullable=True),
    )
    op.add_column(
        "memory_shards",
        sa.Column("attributed_project_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_memory_shards_actor_user_id",
        "memory_shards",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_memory_shards_attributed_project_id",
        "memory_shards",
        "projects",
        ["attributed_project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_memory_shards_reach", "memory_shards", ["reach"])

    op.create_table(
        "lesson_outcomes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shard_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("related_item_id", sa.String(), nullable=True),
        sa.Column("related_shard_id", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["shard_id"], ["memory_shards.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lesson_outcomes_shard_id", "lesson_outcomes", ["shard_id"])


def downgrade() -> None:
    op.drop_index("ix_lesson_outcomes_shard_id", table_name="lesson_outcomes")
    op.drop_table("lesson_outcomes")
    op.drop_index("ix_memory_shards_reach", table_name="memory_shards")
    op.drop_constraint("fk_memory_shards_attributed_project_id", "memory_shards", type_="foreignkey")
    op.drop_constraint("fk_memory_shards_actor_user_id", "memory_shards", type_="foreignkey")
    op.drop_column("memory_shards", "attributed_project_id")
    op.drop_column("memory_shards", "actor_user_id")
    op.drop_column("memory_shards", "lesson_class")
    op.drop_column("memory_shards", "reach")
