"""PRD-43 live feedback kit: ingest token, surface flags, boards, comments, votes.

Adds the public-surface columns and the request_comments / request_votes tables.
SQLite tests use create_all; this chain is what Postgres runs from empty.

Revision ID: 0129
Revises: 0128
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0129"
down_revision: Union[str, None] = "0128"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("public_host", sa.String(), nullable=True))
    op.create_unique_constraint("uq_organizations_public_host", "organizations", ["public_host"])
    op.add_column(
        "organizations",
        sa.Column("public_host_custom", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "organizations",
        sa.Column("feedback_default_on", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column("platform_config", sa.Column("ingest_token_hash", sa.String(), nullable=True))
    op.add_column(
        "platform_config",
        sa.Column("ingest_token_prefix", sa.String(), nullable=False, server_default=""),
    )
    op.add_column("platform_config", sa.Column("public_path_id", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_platform_config_public_path_id", "platform_config", ["public_path_id"]
    )
    for flag in (
        "intake_enabled",
        "public_form_enabled",
        "public_roadmap_enabled",
        "public_issues_enabled",
        "public_requests_enabled",
        "capture_identity",
    ):
        op.add_column(
            "platform_config",
            sa.Column(flag, sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    op.add_column("requests", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("requests", sa.Column("track_token_hash", sa.String(), nullable=True))
    op.create_unique_constraint("uq_requests_track_token_hash", "requests", ["track_token_hash"])

    op.create_table(
        "request_comments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("request_id", sa.String(), sa.ForeignKey("requests.id"), nullable=False, index=True),
        sa.Column("author_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("visibility", sa.String(), nullable=False, server_default="private"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "request_votes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(), sa.ForeignKey("requests.id"), nullable=False, index=True),
        sa.Column("voter_key", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("request_id", "voter_key", name="uq_request_vote"),
    )


def downgrade() -> None:
    op.drop_table("request_votes")
    op.drop_table("request_comments")
    op.drop_constraint("uq_requests_track_token_hash", "requests", type_="unique")
    op.drop_column("requests", "track_token_hash")
    op.drop_column("requests", "published_at")
    for flag in (
        "capture_identity",
        "public_requests_enabled",
        "public_issues_enabled",
        "public_roadmap_enabled",
        "public_form_enabled",
        "intake_enabled",
    ):
        op.drop_column("platform_config", flag)
    op.drop_constraint("uq_platform_config_public_path_id", "platform_config", type_="unique")
    op.drop_column("platform_config", "public_path_id")
    op.drop_column("platform_config", "ingest_token_prefix")
    op.drop_column("platform_config", "ingest_token_hash")
    op.drop_column("organizations", "feedback_default_on")
    op.drop_column("organizations", "public_host_custom")
    op.drop_constraint("uq_organizations_public_host", "organizations", type_="unique")
    op.drop_column("organizations", "public_host")
