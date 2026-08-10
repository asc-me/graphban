"""Usage telemetry and retirement for generated artifacts (GRPH-309 / PRD-16).

Two tables, and the interesting constraint is what does NOT get written.

`artifact_usage` is only ever written by something that OBSERVED a use: a skill invoked by
name, an agent spawned, or a generated hook reporting its own firing. Never inferred from
transcripts. PRD-16: *"A fabricated signal here deletes working hooks."* An artifact that
works produces no evidence it works — a rule everyone follows is mentioned least of all —
so the absence of a row means "not observed", never "not used".

That is why the measurement gap was settled per-tier rather than uniformly (decided
2026-08-10): skills and agents are observable from first-party MCP metering; HOOKS are
instrumented at generation, since we render the script and it can report its own firing;
RULES are accepted as unmeasurable and excluded from staleness entirely, because compliance
leaves no trace by construction.

`artifact_tombstones` keeps the full contents. A retirement that discarded them would make
the decision irreversible on the strength of a usage count — and a usage count is exactly
the kind of evidence that turns out to have been measuring the wrong thing.

Revision ID: 0061
Revises: 0060
"""
from alembic import op
import sqlalchemy as sa

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("recommendation_id", sa.Integer(), nullable=False),
        sa.Column("signal", sa.String(), nullable=False, server_default=""),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_artifact_usage_rec", "artifact_usage", ["recommendation_id"])
    op.create_index("ix_artifact_usage_ts", "artifact_usage", ["ts"])
    op.create_table(
        "artifact_tombstones",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("recommendation_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(), nullable=False, server_default=""),
        sa.Column("contents", sa.Text(), nullable=False, server_default=""),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_artifact_tombstones_rec", "artifact_tombstones",
                    ["recommendation_id"])


def downgrade() -> None:
    op.drop_index("ix_artifact_tombstones_rec", table_name="artifact_tombstones")
    op.drop_table("artifact_tombstones")
    op.drop_index("ix_artifact_usage_ts", table_name="artifact_usage")
    op.drop_index("ix_artifact_usage_rec", table_name="artifact_usage")
    op.drop_table("artifact_usage")
