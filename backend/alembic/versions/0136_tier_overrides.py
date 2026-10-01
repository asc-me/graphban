"""A deployment's tier map: which model each harness runs for a tier (GRPH-1003).

`Build.dc.html` draws the tier map as an editable panel — per-cell override, inherit from
performance grading, clear, save — and what shipped beside it was a read-only catalog. The
information was there and the control was not, so the map was edited by hand in
`fleet/src/gbfleet/matrix.toml`: TOML package data inside the published wheel, which meant a
deployment could not retune its own fleet without a release.

One row per (project, harness, tier) cell. The packaged matrix stays the DEFAULT and this
layers on top of it, so a cell with no row resolves exactly as it did before this migration
and an existing deployment is unaffected.

Clearing the map deletes rows rather than writing empty ones, and that is the load-bearing
choice: "cleared" and "never set" become the same state, so neither can be served as an empty
tier map. An empty map would route nothing and read as a clean result.

The unique index is the cell. Two rows for one cell would make "which model does this harness
run for this tier" a question with two answers, and the supervisor reads it once at wave start.

Revision ID: 0136
Revises: 0135
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0136"
down_revision: Union[str, None] = "0135"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tier_overrides",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("harness", sa.String(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tier_overrides_project_id", "tier_overrides", ["project_id"])
    op.create_index("ix_tier_overrides_cell", "tier_overrides",
                    ["project_id", "harness", "tier"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tier_overrides_cell", table_name="tier_overrides")
    op.drop_index("ix_tier_overrides_project_id", table_name="tier_overrides")
    op.drop_table("tier_overrides")
