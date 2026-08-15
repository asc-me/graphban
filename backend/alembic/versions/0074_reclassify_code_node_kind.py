"""Reclassify `code_nodes.kind` from the path's shape (GRPH-382).

The enum `module | file | symbol` shipped without definitions, so callers guessed — and guessed
the same way almost every time. Measured on the live instance: **119 of 123 nodes labelled
`module`, 4 labelled `file`, 0 `symbol` — while every single path was a file path** like
`backend/app/services/clustering.py`. The four honest `file` rows were one describe pass that
happened to use the field correctly.

Nothing read `kind` except a colour, which is why it drifted for so long without anyone
noticing. PRD-20 D5 changes that: it encodes kind in node fill and argues presence clouds are
safe precisely because they use a DIFFERENT channel. On a graph that is 97% one value that
argument defends something that is not there, so the population has to be right before the
design it supports is worth anything.

**Inferred from the path, not guessed again.** `path::name` is a symbol, a path ending in a
source suffix is a file, anything else is a package. That is now `code_graph.kind_for_path`,
the same function `upsert_node` validates against, so this migration and every future write
agree by construction rather than by coincidence.

Data-only and idempotent: re-running changes nothing, because it computes from a path that has
not moved. `down_revision` restores nothing on purpose — the previous values were wrong, and a
downgrade that faithfully reinstated 119 mislabelled rows would be restoring the defect.

Revision ID: 0074
Revises: 0073
"""
from alembic import op
import sqlalchemy as sa

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

# Mirrors code_graph._SOURCE_SUFFIXES. Duplicated deliberately: a migration must keep meaning
# what it meant on the day it ran, and importing app code would let a later edit silently
# change what this already did to a deployed database.
_SOURCE_SUFFIXES = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java",
    ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".sh", ".sql",
    ".css", ".scss", ".html", ".vue", ".svelte", ".toml", ".yaml", ".yml", ".json", ".md",
)


def _kind_for_path(path: str) -> str:
    p = (path or "").strip()
    if "::" in p:
        return "symbol"
    if p.lower().endswith(_SOURCE_SUFFIXES):
        return "file"
    return "module"


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, path, kind FROM code_nodes")).fetchall()
    for node_id, path, kind in rows:
        implied = _kind_for_path(path or "")
        if implied != kind:
            conn.execute(
                sa.text("UPDATE code_nodes SET kind = :k WHERE id = :i"),
                {"k": implied, "i": node_id},
            )


def downgrade() -> None:
    """Deliberately a no-op.

    The values this replaced were wrong — a downgrade that faithfully reinstated them would be
    restoring the defect, and the correct kind is recomputable from the path at any time.
    """
