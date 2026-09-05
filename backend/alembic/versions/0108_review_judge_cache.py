"""Cache the review judge's verdict on the shard it judged.

One GET of the memory review queue could ask the chat model up to 24 times
(REVIEW_JUDGE_MAX=8 candidates x JUDGE_SAMPLES=3), uncached, every single load. Measured
on the live Ollama host (ms-s1-ubt): requests there are served strictly one at a time —
8 concurrent generations complete at 4.2s, 8.4, 12.6 ... 33.7s, perfectly linear — so
that sweep occupies the only slot for ~100s while an interactive grill, whose budget is
90s, simply waits behind it.

Keyed on a digest of the context the judge was shown plus the model, so the cache is only
reused for the identical question. Successful verdicts only.

Revision ID: 0108
Revises: 0107
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0108"
down_revision: Union[str, None] = "0107"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memory_shards", sa.Column("review_judge_key", sa.String(),
                                             nullable=False, server_default=""))
    op.add_column("memory_shards", sa.Column("review_judge_verdict", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_shards", "review_judge_verdict")
    op.drop_column("memory_shards", "review_judge_key")
