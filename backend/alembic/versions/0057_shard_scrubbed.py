"""Record that a shard went through the redactor (GRPH-305 / PRD-16).

PRD-16: *"Record that scrubbing ran, so an unscrubbed legacy row is distinguishable from a
clean one."*

Existing rows default to False, which is the honest reading: they predate the redactor and
nobody knows what is in them. Backfilling True would assert a check that never happened,
and inferring it from the text looking fine is the same mistake — an absence of visible
secrets is not evidence of scrubbing.

Revision ID: 0057
Revises: 0056
"""
from alembic import op
import sqlalchemy as sa

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_shards", sa.Column("scrubbed", sa.Boolean(), nullable=False,
                                             server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("memory_shards", "scrubbed")
