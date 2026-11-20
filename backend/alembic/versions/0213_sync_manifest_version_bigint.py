"""Widen sync_manifest.version to BIGINT (GRPH-1768).

`version_of()` is a signed 63-bit int. Migration 0211 stored it as INTEGER,
which is int4 on Postgres, so a confirmed payload raises
NumericValueOutOfRange. SQLite does not enforce the width.
`sync_outbox.version` is a capture counter and stays INTEGER.

Revision ID: 0213
Revises: 0212
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0213"
down_revision: Union[str, None] = "0212"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sync_manifest") as batch:
        batch.alter_column(
            "version",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
            existing_server_default="0",
        )


def downgrade() -> None:
    with op.batch_alter_table("sync_manifest") as batch:
        batch.alter_column(
            "version",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
            existing_server_default="0",
        )
