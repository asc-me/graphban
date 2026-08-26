"""A project may override the model without a second copy of the key (PRD-25 S2).

Wanting the shared credential on a cheaper model previously meant a second credentials row
holding the same secret — which stores one key twice, so rotating it is two edits and one of
them gets forgotten. An override column keeps the secret single.

Empty string, not NULL: "" and NULL would both mean "no override" and the code would have to
treat them alike forever. One representation.

Revision ID: 0086
Revises: 0085
"""
import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("model_override", sa.String(),
                                        nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("projects", "model_override")
