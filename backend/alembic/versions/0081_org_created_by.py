"""Who founded the org, recorded rather than inferred (PRD-21 D8.2, GRPH-419).

Hand-written, as 0077-0080 were.

Ownership used to be readable only as "whoever holds the `owner` seat", which was safe
while that seat was immutable. D8.1 makes it demotable, so the inference breaks: an org
whose founder stepped back would report a different founder, or none.

Backfilled from the current owner seat, which is exactly right for every org that exists
today — none of them can have transferred ownership, because until now nothing could.
Nullable, so an org with no owner seat at all backfills to NULL rather than failing.

Revision ID: 0081
Revises: 0080
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0081"
down_revision: Union[str, None] = "0080"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("created_by", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_organizations_created_by", "organizations", "users", ["created_by"], ["id"]
    )
    op.execute(sa.text("""
        UPDATE organizations SET created_by = (
            SELECT user_id FROM org_memberships
            WHERE org_memberships.org_id = organizations.id
              AND org_memberships.role = 'owner'
            ORDER BY org_memberships.created_at
            LIMIT 1
        )
    """))


def downgrade() -> None:
    op.drop_constraint("fk_organizations_created_by", "organizations", type_="foreignkey")
    op.drop_column("organizations", "created_by")
