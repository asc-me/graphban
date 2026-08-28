"""A key carries the tool tiers it was minted with (GRPH-571).

Hand-written for the reason 0077-0080 and 0092 were: `--autogenerate` sweeps in unrelated
drift between the models and the chain.

One nullable JSON column. **NULL is not backfilled, and that is the decision.** Every earlier
column of this shape was backfilled to preserve behaviour — `roles` to all three so nothing in
flight broke — and doing that here would defeat the change entirely: every key on a running
deployment predates this migration, so backfilling them to all tiers means the manifest never
shrinks for anyone who already has a key. `tool_tiers.visible` therefore reads NULL as "core
only" rather than "everything".

What that costs is real and worth writing down rather than discovering: an existing key's
manifest gets smaller the moment this deploys. It does NOT lose the ability to call anything —
the manifest is not an authorisation boundary and `_call_tool` is untouched — but an agent
choosing from what it was shipped will stop choosing the tiered tools. `get_context` names the
tiers it is missing and how to get them, and `MCP_DEFAULT_TOOL_TIERS` restores the old manifest
for a whole deployment without a code change, for an operator who wants the old behaviour back
while they re-mint.

Revision ID: 0093
Revises: 0092
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0093"
down_revision: Union[str, None] = "0092"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("tool_tiers", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "tool_tiers")
