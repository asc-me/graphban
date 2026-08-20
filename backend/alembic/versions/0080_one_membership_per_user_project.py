"""One membership per (user, project) — PRD-21 AC 9e (GRPH-420).

Hand-written, for the reason 0077 and 0078 were: `--autogenerate` sweeps in unrelated
drift between the models and the chain.

**The sweep runs first and is the whole risk of this migration.** `teams.recompute` has
been writing derived rows since PRD-21 D5 shipped with no uniqueness to stop it, so a
production table may already hold duplicates. Adding the constraint to a table that has
them fails the migration and takes the API down on startup, which is how a correctness fix
becomes an outage.

Where a pair has more than one row, one is kept by a stated rule rather than by accident:

1. `direct` beats `team`, **whatever the access levels are**. This mirrors
   `teams.recompute`, which returns a direct row untouched and materialises nothing over
   it — so the sweep converges on exactly the state recompute would produce for that pair
   rather than on a state recompute would immediately undo.

   It follows that the sweep CAN lower someone's effective access: a direct `read`
   outranks a team `write`. That is the correct answer, not a regrettable one — under D5
   the team grant was never in force for that person to begin with.
2. Then the highest access, **within an origin tier** — where two team rows disagree, the
   more permissive survives, matching recompute's highest-wins rule across grants.
3. Then the lowest id, so the result is deterministic and a re-run is a no-op.

Revision ID: 0080
Revises: 0079
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0080"
down_revision: Union[str, None] = "0079"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEDUPE = """
DELETE FROM memberships WHERE id NOT IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY user_id, project_id
            ORDER BY
                CASE origin WHEN 'direct' THEN 0 ELSE 1 END,
                CASE access WHEN 'write' THEN 0 WHEN 'read' THEN 1 ELSE 2 END,
                id
        ) AS rn
        FROM memberships
    ) ranked WHERE rn = 1
)
"""


def upgrade() -> None:
    op.execute(sa.text(_DEDUPE))
    with op.batch_alter_table("memberships") as batch:
        batch.create_unique_constraint("uq_membership_user_project", ["user_id", "project_id"])


def downgrade() -> None:
    # Deliberately drops only the constraint. The rows the sweep removed are not restored:
    # they were duplicates that should never have existed, and inventing replacements for
    # them would be fabricating access.
    with op.batch_alter_table("memberships") as batch:
        batch.drop_constraint("uq_membership_user_project", type_="unique")
