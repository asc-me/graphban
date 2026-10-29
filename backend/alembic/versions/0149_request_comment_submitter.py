"""`request_comments.author_kind` and a nullable author: submitter replies (PRD-60 D9 / GRPH-1139).

A submitter has no user row; their tracking token is the credential. Existing rows are all
operator comments, which the server default covers.

Revision ID: 0149
Revises: 0148
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0149"
down_revision: Union[str, None] = "0148"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "request_comments",
        sa.Column("author_kind", sa.String(), nullable=False, server_default="operator"),
    )
    op.alter_column("request_comments", "author_user_id", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM request_comments WHERE author_user_id IS NULL")
    op.alter_column("request_comments", "author_user_id", existing_type=sa.String(), nullable=False)
    op.drop_column("request_comments", "author_kind")
