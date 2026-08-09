"""The platform judge's classifications (GRPH-249 / PRD-12).

Server-side classification of completed work against the PRD goal: serves / enables /
unrelated / undecidable. Event-driven, firing when an item is marked `done` — at link time
an item is an intention with nothing delivered to judge; at completion it has evidence,
touchpoints and work behind it.

One row per item, not a history. This is a DERIVED read and what matters is the current
answer; the append-only audit trail lives on `verdicts`, which holds claims rather than
derivations.

`stale` is the single source of truth for recomputation, so the eager path (three items or
fewer, inline) and the lazy path (on first read, cached after) agree by construction rather
than by care. `baseline_version` is what makes staleness computable at all: a baseline
change invalidates prior judgements, and a row with no baseline stamped could never be
known to be current.

`graded_by` records which bar was applied. A stub-graded row means "not assessed", which
without this column is indistinguishable from "assessed and found fine" — the AL-299 rule,
one surface over.

Revision ID: 0054
Revises: 0053
"""
from alembic import op
import sqlalchemy as sa

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_classifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.String(), sa.ForeignKey("items.id"), nullable=False),
        sa.Column("prd_id", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("graded_by", sa.String(), nullable=False, server_default=""),
        sa.Column("baseline_version", sa.String(), nullable=False, server_default=""),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("item_id", name="uq_classification_item"),
    )
    op.create_index("ix_work_classifications_item_id", "work_classifications", ["item_id"])
    op.create_index("ix_work_classifications_prd_id", "work_classifications", ["prd_id"])


def downgrade() -> None:
    op.drop_index("ix_work_classifications_prd_id", table_name="work_classifications")
    op.drop_index("ix_work_classifications_item_id", table_name="work_classifications")
    op.drop_table("work_classifications")
