"""Truncated model output on llm_call_spans for human-eval sampling (GRPH-644).

Spans were telemetry-only (GRPH-225): tokens, cost, feature, ok. Sampling them
into Memory review with no text would fill the queue with un-labelable rows —
an absence reading as work. This column is the smallest payload that makes a
span reviewable. NULL is "nothing to label", never "".

The prompt is not stored. A span is not a transcript.

Revision ID: 0098
Revises: 0097
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0098"
down_revision: Union[str, None] = "0097"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_call_spans",
        sa.Column("output_preview", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_call_spans", "output_preview")
