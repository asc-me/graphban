"""What an agent was last refused for, on the agent (GRPH-774).

`record_refusal` counted refusals and quarantined at three, and the reason went only to the
events table — so the Fleet view showed "idle worker" for an agent being told no on every
planner-only call it made. Diagnosing one took a database query.

A column rather than another key in `capabilities`: that dict is what the CHILD declares about
itself (vendor, model, instance — GRPH-732 reads it), and a server verdict living inside it is
echoed back as though the agent had claimed it. The count already sits there and should not
have; this is the half that decides not to make it worse.

Revision ID: 0115
Revises: 0114
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0115"
down_revision: Union[str, None] = "0114"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("last_refusal", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "last_refusal")
