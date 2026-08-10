"""Incremental transcript ingest (GRPH-304 / PRD-16).

*"Incremental and watermarked: a re-run must not duplicate evidence."* A transcript is
append-only and long; re-reading it from the top on every run would multiply the evidence
behind every lesson, and the promotion ladder counts that evidence to decide what is real.
Duplicated evidence does not just waste work — it manufactures corroboration, promoting a
lesson that only ever happened once.

Keyed by (adapter, source) so two harnesses reading the same path do not overwrite each
other's progress. The watermark string is opaque to everything but the adapter that wrote
it: a line count for append-only JSONL, a byte offset or a cursor elsewhere.

Revision ID: 0058
Revises: 0057
"""
from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_watermarks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("adapter", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("watermark", sa.String(), nullable=False, server_default=""),
        sa.Column("events_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("adapter", "source", name="uq_ingest_source"),
    )


def downgrade() -> None:
    op.drop_table("ingest_watermarks")
