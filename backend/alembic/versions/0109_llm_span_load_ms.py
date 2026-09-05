"""How much of a call was the model loading, when the provider says.

Ollama reports `load_duration` on the same final line the adapter already parses for
token counts, and it was being dropped. Measured on ms-s1-ubt: a cold call spends 10.24s
of 11.86s loading — 86% — and a warm one reports a real 0.0. Without the column there is
no way to answer "are we paying reloads", which is the only question that makes
OLLAMA_KEEP_ALIVE decidable.

NULL is "nobody said", never zero: a provider that does not report loading is not a
provider that loaded instantly.

Revision ID: 0109
Revises: 0108
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0109"
down_revision: Union[str, None] = "0108"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("llm_call_spans", sa.Column("load_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_call_spans", "load_ms")
