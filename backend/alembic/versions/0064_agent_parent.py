"""Declared parentage on an agent (GRPH-361 / PRD-17 D3).

PRD-17 §9 claimed an in-session verifier subagent "structurally cannot" satisfy the reviewer
gate because it shares its parent's identity. It does not: `register_agent` mints a row per
call — deliberately, because "two terminals on one key are two agents" is the bug D1 exists to
fix — so a subagent that registers becomes a sibling with a distinct id and can review its
parent's work.

`parent_agent_id` lets a subagent say what it is. It is self-reported, like `worktree` and
`vendor`, and the undeclared case is covered separately by treating same-credential +
same-host as non-independent.

Revision ID: 0064
Revises: 0063
"""
from alembic import op
import sqlalchemy as sa

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("parent_agent_id", sa.String(), nullable=True))
    op.create_index("ix_agents_parent", "agents", ["parent_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_parent", table_name="agents")
    op.drop_column("agents", "parent_agent_id")
