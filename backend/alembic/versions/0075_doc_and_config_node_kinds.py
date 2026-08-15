"""Reclassify docs and config out of `file` (GRPH-381).

0074 gave `code_nodes.kind` definitions and corrected 119 files mislabelled as modules. It left
a narrower problem in place: `.md`, `.yml`, `.toml` and `.json` were all in the CODE suffix
table, so a documentation file and a Python module were the same kind of thing to the graph.

They are not, and the gap was measurable. 15 of 100 touchpoints on live backlog items resolved
to no node at all, and the set was dominated by exactly these files — `docs/mcp.md` twice,
`AGENTS.md`, `README.md`, `web/nginx.conf`, `.cursor/rules/*`. They are load-bearing: the rules
file every agent reads, the tool contract, the thing that primes editor agents. Work touching
them is precisely the work whose blast radius a human wants to see.

**This does not by itself shrink PRD-20's off-map tray, and the distinction matters.** A kind
makes those files DESCRIBABLE; someone still has to describe them. What it removes is the
blocker — before this there was no bucket to put them in, so the tray could never empty no
matter how thorough a describe pass was.

Same shape as 0074: computed from `kind_for_path`, which is also what the write path validates
against, so migration and future writes agree by construction. Idempotent — it recomputes from
a path that has not moved. `downgrade` restores nothing, because `doc` and `config` did not
exist before and collapsing them back into `file` would reinstate the conflation.

Revision ID: 0075
Revises: 0074
"""
from alembic import op
import sqlalchemy as sa

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

# Mirrors code_graph's tables. Duplicated on purpose: a migration must keep meaning what it
# meant on the day it ran, and importing app code would let a later edit silently change what
# this already did to a deployed database.
_DOC = (".md", ".mdx", ".rst", ".txt", ".adoc")
_CONFIG = (
    ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ".env", ".properties",
    ".lock", ".mdc",
)
_CODE = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java",
    ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".sh", ".sql",
    ".css", ".scss", ".html", ".vue", ".svelte",
)
_CONFIG_BASENAMES = frozenset({
    "dockerfile", "makefile", "procfile", "caddyfile", "justfile", "brewfile", ".gitignore",
    ".dockerignore", ".editorconfig", ".gitattributes", ".npmrc", ".nvmrc",
})


def _kind_for_path(path: str) -> str:
    p = (path or "").strip()
    if "::" in p:
        return "symbol"
    low = p.lower()
    base = low.rsplit("/", 1)[-1]
    if low.endswith(_DOC):
        return "doc"
    if low.endswith(_CONFIG) or base in _CONFIG_BASENAMES:
        return "config"
    if low.endswith(_CODE):
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

    `doc` and `config` did not exist before this, so a faithful downgrade would collapse them
    back into `file` — reinstating exactly the conflation this removes. The correct kind is
    recomputable from the path at any time.
    """
