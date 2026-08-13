"""Enrolment seats: a role for one session, separate from the credential (GRPH-368 / PRD-19 E1).

An MCP client sends ONE static header, and that string has been carrying identity, ceiling,
role and wave together. Two of those change rarely and two change every run, which is why a
fleet needed a credential per role and a client storing one config for every agent could not
run a fleet at all.

`enrolments` is the ephemeral half. A seat grants one role on one project for one session and
expires; the credential stays put in the config.

NULL `agents.enrolment_id` is the migration position and the DEFAULT: un-enrolled means the
single-agent posture, so every existing agent keeps behaving exactly as it does today.

Hand-written, like every migration here — `--autogenerate` proposes dropping the HNSW vector
indexes.

Revision ID: 0066
Revises: 0065
"""
from alembic import op
import sqlalchemy as sa

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enrolments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("code_prefix", sa.String(), server_default="", nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("wave", sa.String(), nullable=True),
        sa.Column("issued_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("minted_by", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.String(), nullable=True),
        sa.Column("reissued_from", sa.String(), nullable=True),
        sa.Column("revoked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_enrolments_project", "enrolments", ["project_id"])
    # Redemption looks a seat up BY HASH on every register_agent that carries a code.
    op.create_index("ix_enrolments_code_hash", "enrolments", ["code_hash"])
    op.create_index("ix_enrolments_wave", "enrolments", ["wave"])
    op.create_index("ix_enrolments_reissued_from", "enrolments", ["reissued_from"])

    op.add_column("agents", sa.Column("enrolment_id", sa.String(), nullable=True))
    op.create_index("ix_agents_enrolment", "agents", ["enrolment_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_enrolment", table_name="agents")
    op.drop_column("agents", "enrolment_id")
    op.drop_index("ix_enrolments_reissued_from", table_name="enrolments")
    op.drop_index("ix_enrolments_wave", table_name="enrolments")
    op.drop_index("ix_enrolments_code_hash", table_name="enrolments")
    op.drop_index("ix_enrolments_project", table_name="enrolments")
    op.drop_table("enrolments")
