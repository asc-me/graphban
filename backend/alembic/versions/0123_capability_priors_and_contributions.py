"""PRD-41 S4: capability priors, contributions, snapshots, instance share.

`capability_priors` is the fetched platform snapshot (D12). `platform_contributions`
and `platform_contribution_cells` are the hosted accept of self-hosted rollups (D11).
`platform_model_sightings` is the accept-time redaction bookkeeping (criterion 29).
`capability_snapshots` is the nightly published prior. SyncLink gains the instance
telemetry_share toggle and the last-contribution facts the sync page shows.

Revision ID: 0123
Revises: 0122
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0123"
down_revision: Union[str, None] = "0122"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "capability_priors",
        sa.Column("vendor", sa.String(32), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("binary_version", sa.String(32), primary_key=True),
        sa.Column("capability", sa.String(8), primary_key=True),
        sa.Column("size_band", sa.String(1), primary_key=True),
        sa.Column("rate", sa.Float(), nullable=True),
        sa.Column("n_band", sa.String(16), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="platform"),
        sa.Column("snapshot_at", sa.String(32), nullable=False, server_default=""),
    )

    op.create_table(
        "capability_snapshots",
        sa.Column("snapshot_at", sa.String(32), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("cell_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "platform_contributions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_version_seen", sa.String(32), nullable=False, server_default=""),
        sa.Column("opted_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("floors", sa.JSON(), nullable=True),
        sa.Column("redacted_models", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_platform_contributions_instance", "platform_contributions",
                    ["instance_id"])

    op.create_table(
        "platform_contribution_cells",
        sa.Column("instance_id", sa.String(), primary_key=True),
        sa.Column("week", sa.String(8), primary_key=True),
        sa.Column("vendor", sa.String(32), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("binary_version", sa.String(32), primary_key=True),
        sa.Column("capability", sa.String(8), primary_key=True),
        sa.Column("size_band", sa.String(1), primary_key=True),
        sa.Column("model_hash", sa.String(32), nullable=False, server_default=""),
        sa.Column("finished", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signed_off", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_choice", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explicit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("probe", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "platform_model_sightings",
        sa.Column("vendor", sa.String(32), primary_key=True),
        sa.Column("model_hash", sa.String(32), primary_key=True),
        sa.Column("instance_id", sa.String(), primary_key=True),
        sa.Column("model_plain", sa.String(64), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column("sync_link", sa.Column(
        "telemetry_share", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("sync_link", sa.Column(
        "last_contribution_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sync_link", sa.Column(
        "last_contribution_rows", sa.Integer(), nullable=True))
    op.add_column("sync_link", sa.Column(
        "last_floors", sa.JSON(), nullable=True))
    op.add_column("sync_link", sa.Column(
        "last_redacted_models", sa.Integer(), nullable=True))
    op.add_column("sync_link", sa.Column(
        "last_snapshot_at", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_link", "last_snapshot_at")
    op.drop_column("sync_link", "last_redacted_models")
    op.drop_column("sync_link", "last_floors")
    op.drop_column("sync_link", "last_contribution_rows")
    op.drop_column("sync_link", "last_contribution_at")
    op.drop_column("sync_link", "telemetry_share")
    op.drop_table("platform_model_sightings")
    op.drop_table("platform_contribution_cells")
    op.drop_index("ix_platform_contributions_instance",
                  table_name="platform_contributions")
    op.drop_table("platform_contributions")
    op.drop_table("capability_snapshots")
    op.drop_table("capability_priors")
