"""Why a verdict is or is not self-signed (GRPH-327 / PRD-12).

Found by auditing PRD-12 through its own agent-auditor surface: 0 of 27 items carried a
`claimed_by`, so `self_signed_against` compared the signer against an empty set and
returned False for all 14 section verdicts — signed by the agent that wrote every one.

The check did not fail. It had nothing to check, and reported that as a pass.

`self_signed: false` conflated two opposite situations: "someone else built this" and
"nobody recorded who built this". `separation` names which:

    self-signed    the signer's fingerprints are on the work
    independent    someone else's are, and they are not the signer's
    unverifiable   nothing records who built it — no claim, no event

The fifth instance in this PRD of an absence rendering as a clean result, after
GRPH-251, GRPH-324, `governed: False`, and GRPH-325. Nothing recorded is not the same as
nothing wrong.

Revision ID: 0056
Revises: 0055
"""
from alembic import op
import sqlalchemy as sa

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("verdicts", sa.Column("separation", sa.String(), nullable=False,
                                        server_default=""))


def downgrade() -> None:
    op.drop_column("verdicts", "separation")
