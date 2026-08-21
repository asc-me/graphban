"""When the BODY last changed, distinct from when the row did (GRPH-430).

A PRD can be grilled to completion and keep a body that still says the older things. Nothing
noticed: GRPH-424 closed *repo copy vs ledger*, and this is the same absence one level in —
*ledger body vs its own grill*. Downstream (`decompose_prd`, `prd_coverage`, the completeness
pass, and anyone reading it) a document that survived its grill without absorbing it is
indistinguishable from one that did.

`prds.updated_at` cannot answer this. It carries `onupdate`, so it moves for any row write —
and answering a grill writes the row whenever the answer changes the derived status
(`draft -> review -> approved`). The body would then look freshly edited precisely at the
moments it had NOT been touched, which is the one case the check exists to catch. So the body
gets its own timestamp, written only when the text actually differs.

Backfilled to `updated_at`: the best available approximation for rows that predate the column,
and deliberately the OPTIMISTIC one. A backfill of NULL or epoch would flag every existing PRD
as stale on day one, and a check that cries wolf about history is one people switch off before
it ever catches anything real.

Revision ID: 0082
Revises: 0081
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0082"
down_revision: Union[str, None] = "0081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prds", sa.Column("body_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(sa.text("UPDATE prds SET body_updated_at = updated_at"))


def downgrade() -> None:
    op.drop_column("prds", "body_updated_at")
