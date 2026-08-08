"""Sign-off verdicts with provenance (GRPH-253 / PRD-12).

PRD-12 is blunt that an agent-side signer *moves* the self-attestation problem rather than
solving it. The mitigation is falsifiability, not trust: a verdict must cite, the citations
must resolve to things that exist, and who signed it is on the record. A verdict is a claim
with provenance, never truth.

`self_signed` is flagged rather than refused. On a solo project the signer and the
implementer are the same person, and refusing there would mean nobody could ever sign off —
the rule would be routed around within a day. `self_signed_items` carries the item keys
that triggered it so the flag is checkable rather than an accusation with nothing behind it.

`outcome` is deliberately unconstrained here. The sign-off taxonomy belongs to the agent
judge (GRPH-252); this table owns admissibility and provenance, and fixing the vocabulary
before the component that uses it exists would be guessing.

Revision ID: 0052
Revises: 0051
"""
from alembic import op
import sqlalchemy as sa

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "verdicts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("prd_id", sa.String(), sa.ForeignKey("prds.id"), nullable=False),
        sa.Column("baseline_version", sa.String(), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("citations", sa.JSON(), nullable=True),
        sa.Column("signed_by", sa.String(), nullable=False, server_default=""),
        sa.Column("api_key_id", sa.String(), nullable=True),
        sa.Column("self_signed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("self_signed_items", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_verdicts_prd_id", "verdicts", ["prd_id"])


def downgrade() -> None:
    op.drop_index("ix_verdicts_prd_id", table_name="verdicts")
    op.drop_table("verdicts")
