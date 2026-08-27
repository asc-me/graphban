"""An item remembers the head it was last seen at (GRPH-555).

The completion gate asks whether an item carries a valid attestation, not whether that
attestation still describes the code. Attest at commit A, push B, and the receipt for A
still opens the gate — including when CI FAILED on B, because a failing run writes no
attestation and leaves the passing one standing.

`head_commit` is what an adapter last observed, written whether the run passed or failed.
The gate compares the two, so a receipt that has been overtaken stops counting.

Empty means no adapter has ever reported, which is the truthful value for every existing
row and for any install without one. Backfilling it with anything would assert an
observation nobody made — the same reasoning migration 0089 gives for `revision`.
"""
import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("head_commit", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("items", "head_commit")
