"""Credentials belong to the deployment, not to a project (PRD-25 S1).

Creates the store and the pointers, and moves NOTHING. The legacy per-project
`platform_config.providers` blob is untouched and stays authoritative — S6 migrates it, and
until then `resolve_chat` reads the blob FIRST (step 0 of the transitional order). That order is
what makes "deploying S1 changes no behaviour" a fact rather than a hope: without it, a project
holding its own key would silently fall through to a deployment default nobody has set, which is
a downgrade to the stub for every project that had configured a real provider.

WHY A TABLE KEYED BY ROW. The blob is keyed by provider id, so two Anthropic keys cannot both
exist — the second overwrites the first. Rows make that a non-question, which is what dissolves
collision detection and the "needs attention" list the earlier design needed (D-a).

WHY `credentials.org_id`. The PRD says these belong to the deployment, which is correct for the
self-hosted posture it was written for. The hosted service is multi-org, and an unowned table
would let one org's project point at another org's key. It mirrors `projects.org_id` — NULL on a
self-hosted install where every project is also NULL, so "the deployment" and "the null org" are
the same set and the PRD's resolution order is unchanged.

WHY `deployment_config.scope` IS `''` AND NOT NULL. It is a primary key, and in Postgres NULL
does not collide with NULL — two rows claiming to be "the deployment" would both be legal and the
second would be invisible. `''` is a real value that exists exactly once.

Revision ID: 0085
Revises: 0084
"""
import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(), nullable=False, server_default=""),
        sa.Column("api_key", sa.String(), nullable=False, server_default=""),
        sa.Column("model", sa.String(), nullable=False, server_default=""),
        sa.Column("state", sa.String(), nullable=False, server_default="pending_validation"),
        sa.Column("validation_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_credentials_org_id", "credentials", ["org_id"])
    op.create_index("ix_credentials_kind", "credentials", ["kind"])
    # S2's retry loop claims due rows by (state, next_attempt_at); the index exists here so the
    # table is never created in a shape the next slice has to migrate again.
    op.create_index("ix_credentials_state", "credentials", ["state"])

    op.create_table(
        "deployment_config",
        sa.Column("scope", sa.String(), primary_key=True),
        sa.Column("default_credential_id", sa.String(),
                  sa.ForeignKey("credentials.id"), nullable=True),
        sa.Column("fallback_credential_id", sa.String(),
                  sa.ForeignKey("credentials.id"), nullable=True),
        sa.Column("embed_credential_id", sa.String(),
                  sa.ForeignKey("credentials.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Named explicitly: SQLite cannot add a FK constraint to an existing table without a full
    # table rebuild, and batch_alter_table is how alembic does that. Postgres takes the plain
    # ALTER. Both engines are supported deployments, so both paths are spelled out.
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("credential_id", sa.String(), nullable=True))
        batch.create_foreign_key(
            "fk_projects_credential_id", "credentials", ["credential_id"], ["id"]
        )
    op.create_index("ix_projects_credential_id", "projects", ["credential_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_credential_id", table_name="projects")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("fk_projects_credential_id", type_="foreignkey")
        batch.drop_column("credential_id")
    op.drop_table("deployment_config")
    op.drop_index("ix_credentials_state", table_name="credentials")
    op.drop_index("ix_credentials_kind", table_name="credentials")
    op.drop_index("ix_credentials_org_id", table_name="credentials")
    op.drop_table("credentials")
