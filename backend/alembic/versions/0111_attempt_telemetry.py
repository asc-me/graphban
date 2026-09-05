"""Attempt telemetry and its rollups (PRD-38 D1, D11, D13).

The whole schema lands in one revision even though the pages that read it arrive over four
PRs. Four migrations for one design would make the arc's shape a property of the migration
chain, and an unused table costs nothing; a half-migrated chain costs a deploy.

`attempt_telemetry` is unique on `delegation_id` AND on `enrolment_id`, which is what makes
the route's upsert a database rule rather than a service habit: two supervisors cannot race a
row into existence twice, whichever of the two posts arrives first.

Revision ID: 0111
Revises: 0110
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0111"
down_revision: Union[str, None] = "0110"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "attempt_telemetry",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("delegation_id", sa.String(), nullable=True, unique=True),
        sa.Column("enrolment_id", sa.String(), nullable=True, unique=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("item_id", sa.String(), sa.ForeignKey("items.id"), nullable=True),
        sa.Column("vendor", sa.String(32), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("binary_version", sa.String(32), nullable=True),
        sa.Column("lane", sa.String(16), nullable=True),
        sa.Column("tier_requested", sa.String(16), nullable=True),
        sa.Column("tier_declared", sa.String(16), nullable=True),
        sa.Column("task_class", sa.String(16), nullable=True),
        sa.Column("size_band", sa.String(1), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=True),
        sa.Column("chosen_winner", sa.String(96), nullable=True),
        sa.Column("chosen_runner_up", sa.String(96), nullable=True),
        sa.Column("chosen_source", sa.String(16), nullable=True),
        sa.Column("sampled", sa.String(16), nullable=True),
        sa.Column("declaration_mismatch", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("bounce_category", sa.String(16), nullable=True),
        sa.Column("claim_to_finish_s", sa.Integer(), nullable=True),
        sa.Column("turns_used", sa.Integer(), nullable=True),
        sa.Column("turn_budget", sa.Integer(), nullable=True),
        sa.Column("wall_seconds", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("exit_meaning", sa.String(64), nullable=True),
        sa.Column("adapter_launched", sa.String(32), nullable=True),
        sa.Column("derived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_attempt_telemetry_project_id", "attempt_telemetry", ["project_id"])
    op.create_index("ix_attempt_telemetry_item_id", "attempt_telemetry", ["item_id"])
    op.create_index("ix_attempt_telemetry_project_finished", "attempt_telemetry",
                    ["project_id", "derived_at"])
    op.create_index("ix_attempt_telemetry_cell", "attempt_telemetry",
                    ["vendor", "model", "lane", "tier_requested"])

    op.create_table(
        "harness_rollups",
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("week", sa.String(8), primary_key=True),
        sa.Column("vendor", sa.String(32), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("binary_version", sa.String(32), primary_key=True),
        sa.Column("lane", sa.String(16), primary_key=True),
        sa.Column("tier", sa.String(16), primary_key=True),
        sa.Column("task_class", sa.String(16), primary_key=True),
        sa.Column("size_band", sa.String(1), primary_key=True),
        sa.Column("finished", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signed_off", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bounced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("median_seconds", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("tokens_reported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_choice", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explicit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rolled_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "platform_rollups",
        sa.Column("week", sa.String(8), primary_key=True),
        sa.Column("vendor", sa.String(32), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("binary_version", sa.String(32), primary_key=True),
        sa.Column("lane", sa.String(16), primary_key=True),
        sa.Column("tier", sa.String(16), primary_key=True),
        sa.Column("task_class", sa.String(16), primary_key=True),
        sa.Column("size_band", sa.String(1), primary_key=True),
        sa.Column("orgs_contributing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finished", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signed_off", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("top_org_share", sa.Float(), nullable=True),
        sa.Column("rolled_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "recommendation_marks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("card_key", sa.String(200), nullable=False),
        sa.Column("evidence_hash", sa.String(32), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_recommendation_marks_user_id", "recommendation_marks", ["user_id"])
    op.create_index("ix_recommendation_marks_scope_id", "recommendation_marks", ["scope_id"])
    op.create_index("ix_recommendation_marks_card", "recommendation_marks",
                    ["user_id", "scope", "scope_id", "card_key"], unique=True)

    op.create_table(
        "harness_lesson_marks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("cell_key", sa.String(200), nullable=False),
        sa.Column("first_crossed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shard_id", sa.String(), nullable=True),
    )
    op.create_index("ix_harness_lesson_marks_project_id", "harness_lesson_marks", ["project_id"])
    op.create_index("ix_harness_lesson_marks_cell", "harness_lesson_marks",
                    ["project_id", "cell_key"], unique=True)

    op.add_column("organizations", sa.Column(
        "telemetry_share", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("organizations", "telemetry_share")
    op.drop_table("harness_lesson_marks")
    op.drop_table("recommendation_marks")
    op.drop_table("platform_rollups")
    op.drop_table("harness_rollups")
    op.drop_table("attempt_telemetry")
