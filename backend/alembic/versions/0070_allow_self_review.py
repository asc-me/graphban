"""Danger mode: may an agent sign off its own work? (GRPH-380)

Off by default. It exists for one configuration — a SOLO all-in-one agent, which now files into
the review pool like every other posture and would otherwise find only its own work waiting
there and stall.

The flag alone never permits self-review: `sign_off` still refuses whenever an independent
agent exists to do the review, because an escape hatch usable while a reviewer is available is
just the review gate switched off. It permits self-review only where the alternative is no
review at all.

Revision ID: 0070
Revises: 0069
"""
from alembic import op
import sqlalchemy as sa

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("allow_self_review", sa.Boolean(), nullable=False,
                                        server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("projects", "allow_self_review")
