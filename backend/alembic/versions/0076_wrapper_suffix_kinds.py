"""Reclassify templated files out of `module` (GRPH-402).

Found by the PRD-20 acceptance walk, on the live instance rather than in a test. The describe
pass asked for `web/nginx.conf.template` as `config`; `upsert_node` corrected it to `module` and
reported the correction, which is the mechanism working — and the answer was wrong.

`kind_for_path` matched on the FINAL suffix, so `.template` beat `.conf` and a templated config
file became a `module`: the one kind that means "this contains other things", which is the shape
a directory has. The same held for `.j2`, `.tmpl`, `.example`, `.sample`, `.dist` and `.in`.

Those suffixes describe how a file is USED, not what it IS, so they are now stripped and the
remainder re-evaluated — which also keeps `settings.py.j2` with CODE, something adding them to
the config table could not have expressed.

Third reclassification in the chain (0074, 0075, this) and computed the same way: from
`kind_for_path`, which is what the write path validates against, so the migration and every
future write agree by construction. Idempotent. `downgrade` restores nothing, because the
previous values were wrong.

Revision ID: 0076
Revises: 0075
"""
from alembic import op
import sqlalchemy as sa

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

# Mirrors code_graph. Duplicated deliberately: a migration must keep meaning what it meant on
# the day it ran, and importing app code would let a later edit silently change what this
# already did to a deployed database.
_WRAPPER = (".template", ".tmpl", ".j2", ".example", ".sample", ".dist", ".in")
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
_ALL = _CODE + _DOC + _CONFIG + _WRAPPER
_CONFIG_BASENAMES = frozenset({
    "dockerfile", "makefile", "procfile", "caddyfile", "justfile", "brewfile", ".gitignore",
    ".dockerignore", ".editorconfig", ".gitattributes", ".npmrc", ".nvmrc",
})


def _kind_for_path(path: str) -> str:
    p = (path or "").strip()
    if "::" in p:
        return "symbol"
    low = p.lower()
    stripped = False
    for _ in range(4):
        for w in _WRAPPER:
            if low.endswith(w) and len(low) > len(w):
                low, stripped = low[: -len(w)], True
                break
        else:
            break
    if stripped and not low.endswith(_ALL):
        return "config"
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
    """Deliberately a no-op — the previous values were wrong, and the correct kind is
    recomputable from the path at any time."""
