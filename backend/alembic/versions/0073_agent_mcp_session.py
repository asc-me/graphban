"""The MCP connection an agent registered over (PRD-19 E9a / GRPH-398).

`tools/list` is fetched once at client connect, before `register_agent` has run, so the
manifest could only ever be trimmed by the CREDENTIAL's ceiling. Under enrolment the
recommended credential is unrestricted — one key, roles come from seats — so nothing trims and
every agent carries the full manifest on every turn for the life of its session.

Binding the connection to the agent is what makes a LATER `tools/list` answerable: the server
can then say which of several agents on one credential is asking.

Deliberately a column on `agents` rather than a session table. An agent belongs to exactly one
connection, and `register_agent` mints a fresh row per call — so a reconnecting client that
registers again produces a new agent anyway, and a session table would only restate that.

Revision ID: 0073
Revises: 0072
"""
from alembic import op
import sqlalchemy as sa

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("mcp_session_id", sa.String(), nullable=True))
    op.create_index("ix_agents_mcp_session", "agents", ["mcp_session_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_mcp_session", table_name="agents")
    op.drop_column("agents", "mcp_session_id")
