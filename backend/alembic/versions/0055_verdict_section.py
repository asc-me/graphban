"""A verdict names the intent element it is about (GRPH-252 / PRD-12).

PRD-12 asks the agent auditor for *"a structured verdict per intent element, each carrying
citations"*. Without a section on the row, one verdict covers a whole PRD and cannot say
which part of it was actually read — an auditor that examined three sections of fourteen
would be indistinguishable from one that examined all of them, and "audited" would mean
nothing more than "somebody submitted something".

The element is the SECTION, the intent atom settled in GRPH-313.

Nullable, because a PRD-level verdict is still a legitimate thing to record — an overall
sign-off alongside the per-section ones. NULL means "about the PRD", not "unknown".

Revision ID: 0055
Revises: 0054
"""
from alembic import op
import sqlalchemy as sa

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("verdicts", sa.Column("section", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("verdicts", "section")
