"""Which model answered, as distinct from which one was asked for (GRPH-993).

The supervisor has been able to read this since GRPH-982: qwen's `-o json` stream opens with
`{"subtype":"init", ..., "model":"<name>"}`, and that name is the EFFECTIVE model rather than
an echo of argv. Measured on 0.23.0, the two come apart:

    qwen -m qwen3.8-max                 ->  init.model = "qwen3.8-max"
    qwen -m definitely-not-a-model-zzz  ->  init.model = "qwen3.7-plus"   (the silent default)

The reading reached the wave summary and stopped there, because `attempt_telemetry` had
nowhere to put it: `model` was already taken, by what the child DECLARED about itself. So
123 measured cells sat filed under `alibaba` + model `""`, and across the 1489 real children
behind them `init.model` said `qwen3.7-plus` 1488 times and `qwen3.8-max` once — every one of
those cells is evidence about qwen3.7-plus, and a preference matrix that cannot separate two
models of one vendor cannot say so.

**No backfill, deliberately.** The 1488/1 count is strong evidence about which model those
cells measured, but it is inference from a population, not a measurement of these rows, and
writing it into the column would put a guess in the one table whose purpose is to be
checkable. Existing rows keep NULL and their cells keep saying `declared`, which is what a
reader needs in order to know why the model is blank: the column did not exist yet. The logs
that could have answered per-row are being swept.

Nullable for the same reason the vendor's own reading is: a child killed mid-write leaves a
truncated stream (69 of 1567 real logs) and an empty one is the ordinary case for a vendor
that prints no result record at all. NULL renders as "not reported" and never as a default.

Revision ID: 0136
Revises: 0135
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0136"
down_revision: Union[str, None] = "0135"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("attempt_telemetry",
                  sa.Column("model_measured", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("attempt_telemetry", "model_measured")
