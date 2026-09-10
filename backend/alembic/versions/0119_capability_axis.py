"""PRD-41 S1: capability axis, diff shape, rollup re-key.

`attempt_telemetry` gains the set, the delegate-time set, the supervisor's diff shape
and tool-error count, and `sampled` already stored a string so `probe` is a new legal
value rather than a new column.

`harness_rollups` and `platform_rollups` take `capability` into the unique key.
`task_class` is backfilled by the D3 map (migration→A4, mcp_tool→B2, frontend→B5,
docs→E3, else `other`) and leaves the key with lane and tier.

Revision ID: 0119
Revises: 0118
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0119"
down_revision: Union[str, None] = "0118"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TASK_CLASS_SQL = """
CASE task_class
    WHEN 'migration' THEN 'A4'
    WHEN 'mcp_tool' THEN 'B2'
    WHEN 'frontend' THEN 'B5'
    WHEN 'docs' THEN 'E3'
    ELSE 'other'
END
"""


def upgrade() -> None:
    op.add_column("attempt_telemetry", sa.Column("capabilities", sa.JSON(), nullable=True))
    op.add_column("attempt_telemetry",
                  sa.Column("capabilities_at_delegate", sa.JSON(), nullable=True))
    op.add_column("attempt_telemetry", sa.Column("diff_shape", sa.JSON(), nullable=True))
    op.add_column("attempt_telemetry", sa.Column("tool_errors", sa.Integer(), nullable=True))

    _rebuild_harness_rollups()
    _rebuild_platform_rollups()


def _rebuild_harness_rollups() -> None:
    op.create_table(
        "_harness_rollups_p41",
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("week", sa.String(8), primary_key=True),
        sa.Column("vendor", sa.String(32), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("binary_version", sa.String(32), primary_key=True),
        sa.Column("capability", sa.String(8), primary_key=True),
        sa.Column("size_band", sa.String(1), primary_key=True),
        sa.Column("lane", sa.String(16), nullable=False, server_default=""),
        sa.Column("tier", sa.String(16), nullable=False, server_default=""),
        sa.Column("task_class", sa.String(16), nullable=False, server_default=""),
        sa.Column("finished", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signed_off", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bounced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("median_seconds", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("tokens_reported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signed_off_reported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_choice", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explicit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("probe", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rolled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(f"""
        INSERT INTO _harness_rollups_p41 (
            project_id, week, vendor, model, binary_version, capability, size_band,
            lane, tier, task_class, finished, signed_off, bounced, median_seconds,
            tokens_in, tokens_out, tokens_reported, signed_off_reported,
            first_choice, fallback, explicit, unknown, probe, rolled_at)
        SELECT project_id, week, vendor, model, binary_version, {_TASK_CLASS_SQL},
               size_band, '', '', '',
               SUM(finished), SUM(signed_off), SUM(bounced),
               AVG(median_seconds),
               SUM(COALESCE(tokens_in, 0)), SUM(COALESCE(tokens_out, 0)),
               SUM(tokens_reported), SUM(COALESCE(signed_off_reported, 0)),
               SUM(first_choice), SUM(fallback), SUM(explicit), SUM(unknown),
               0, MAX(rolled_at)
        FROM harness_rollups
        GROUP BY project_id, week, vendor, model, binary_version, {_TASK_CLASS_SQL},
                 size_band
    """)
    op.drop_table("harness_rollups")
    op.rename_table("_harness_rollups_p41", "harness_rollups")


def _rebuild_platform_rollups() -> None:
    op.create_table(
        "_platform_rollups_p41",
        sa.Column("week", sa.String(8), primary_key=True),
        sa.Column("vendor", sa.String(32), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("binary_version", sa.String(32), primary_key=True),
        sa.Column("capability", sa.String(8), primary_key=True),
        sa.Column("size_band", sa.String(1), primary_key=True),
        sa.Column("lane", sa.String(16), nullable=False, server_default=""),
        sa.Column("tier", sa.String(16), nullable=False, server_default=""),
        sa.Column("task_class", sa.String(16), nullable=False, server_default=""),
        sa.Column("orgs_contributing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finished", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signed_off", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("top_org_share", sa.Float(), nullable=True),
        sa.Column("rolled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(f"""
        INSERT INTO _platform_rollups_p41 (
            week, vendor, model, binary_version, capability, size_band,
            lane, tier, task_class, orgs_contributing, finished, signed_off,
            top_org_share, rolled_at)
        SELECT week, vendor, model, binary_version, {_TASK_CLASS_SQL}, size_band,
               '', '', '',
               MAX(orgs_contributing), SUM(finished), SUM(signed_off),
               MAX(top_org_share), MAX(rolled_at)
        FROM platform_rollups
        GROUP BY week, vendor, model, binary_version, {_TASK_CLASS_SQL}, size_band
    """)
    op.drop_table("platform_rollups")
    op.rename_table("_platform_rollups_p41", "platform_rollups")


def downgrade() -> None:
    """Restore the pre-capability key, and leave the rollups for `harness.roll` to rebuild.

    The re-key merged cells and no downgrade can split them — but a rollup is a CACHE, not a
    record: `harness.roll` recomputes `harness_rollups` from `attempt_telemetry`, which this
    downgrade does not touch, and `roll_if_stale` does it on the next read. So the honest
    downgrade restores the old shape and writes no rows, rather than fabricating a split it
    cannot justify.

    It used to raise instead. That made this the only one of the repository's 72 migrations
    that could not be walked back, and four tests that downgrade PAST it to exercise much
    older backfills (`test_cross_reference_keys`, `test_project_tags`) failed on Postgres —
    the engine CI runs and the seat that built this slice had no container for.
    """
    op.drop_table("harness_rollups")
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
        sa.Column("signed_off_reported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_choice", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explicit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rolled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_table("platform_rollups")
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
    for column in ("tool_errors", "diff_shape", "capabilities_at_delegate", "capabilities"):
        op.drop_column("attempt_telemetry", column)
