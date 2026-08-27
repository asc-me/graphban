"""Items remember the section text they were decomposed from (GRPH-360).

`decompose_prd` copies a section's markdown into the item's description and that copy is
never refreshed. Edit the PRD afterwards and the item holds the old rules forever, silently
— found live on PRD-17, where nine of eleven items had drifted from the approved body while
`prd_coverage` read 100% covered, because it matches items to sections by NAME.

Two columns, not one. `prd_section_hash` is what the section looked like at decompose time.
`prd_section_ack` is a hash a human has said is fine to differ from — kept separately so
acknowledging a divergence silences THAT divergence and not the next one.

Both default to empty, and empty means UNKNOWN rather than clean: every item that predates
this column has no fingerprint, and reporting those as agreeing with their section would
reproduce the exact defect this exists to remove.

Revision ID: 0087
Revises: 0086
"""
import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("prd_section_hash", sa.String(),
                                     nullable=False, server_default=""))
    op.add_column("items", sa.Column("prd_section_ack", sa.String(),
                                     nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("items", "prd_section_ack")
    op.drop_column("items", "prd_section_hash")
