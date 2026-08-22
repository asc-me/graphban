#!/usr/bin/env python3
"""What the reference docs cover, and what they miss (GRPH-475).

Three documents present themselves as complete inventories and are not. Measured 2026-08-22:

    docs/api-reference.md    87 of 160 routes
    docs/configuration.md    19 of  51 settings
    docs/data-model.md       15 of  48 tables

(As this module counts them — two settings are permanently exempt below, so the numbers are
one or two higher than a naive grep of the same documents reports.)

None of them rotted through carelessness. They were accurate when written, the app grew, and
nothing connected the two. The contrast that makes the diagnosis: every fact in this repo that
IS ratcheted — the tool count in `docs/mcp.md` and `AGENTS.md`, the migration range, the PRD
index — is correct today. Accuracy here tracks enforcement, not authorship care.

So this module owns the AUTHORITATIVE SET for each kind, and the rule for what counts as
"mentioned". The generator and the test share it deliberately: a test that re-derives the
extraction rule cannot catch the rule being wrong, which is the lesson `gen_prd_index.py`
already records.

WHAT THIS DELIBERATELY DOES NOT DO. It does not check that a mention is CORRECT — only that
it exists. A route documented with the wrong verb, a setting with the wrong default, a table
with the wrong purpose all pass here. Naming that is the point: this closes the gap where the
docs are silently partial, and leaves the gap where they are wrong. The second needs reading,
not counting.

Usage:  scripts/docs_completeness.py            # print the current gaps
        scripts/docs_completeness.py --write    # regenerate the baseline
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "completeness-baseline.json"

DOCS = {
    "routes": ROOT / "docs" / "api-reference.md",
    "settings": ROOT / "docs" / "configuration.md",
    "tables": ROOT / "docs" / "data-model.md",
}

# Facts that must NEVER appear in these documents, each with the reason. Distinct from the
# baseline: the baseline is a debt to pay down, this is a decision. An entry here that stops
# being real fails the suite, so a stale exemption cannot sit unnoticed.
PERMANENT = {
    "settings": {
        "GIT_SHA": "injected by the build; an operator never sets it",
        "RAILWAY_GIT_COMMIT_SHA": "injected by the platform; an operator never sets it",
    },
    "routes": {},
    # Empty on purpose. `alembic_version` was the obvious candidate and is NOT here: it is
    # created by Alembic and never enters `Base.metadata`, so exempting it would have been a
    # claim about something the authoritative set does not contain. The exemption test caught
    # that on the first run, which is the argument for having it.
    "tables": {},
}


# ---- authoritative sets -----------------------------------------------------------------

def _app():
    os.environ.setdefault("DATABASE_URL", "sqlite:///./.docscheck.db")
    sys.path.insert(0, str(ROOT / "backend"))
    from app.main import app  # noqa: PLC0415
    return app


def live_routes() -> set[str]:
    """Every path the app serves, `{param}` flattened so a rename of the parameter is not
    mistaken for a new endpoint."""
    return {re.sub(r"\{[^}]+\}", "{}", p) for p in _app().openapi()["paths"]}


def live_settings() -> set[str]:
    """The env var each settings field reads, which is what an operator actually types."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.config import Settings  # noqa: PLC0415

    prefix = (Settings.model_config.get("env_prefix") or "")
    out = set()
    for name, f in Settings.model_fields.items():
        alias = getattr(f, "validation_alias", None) or getattr(f, "alias", None)
        out.add((alias if isinstance(alias, str) else prefix + name).upper())
    return out


def live_tables() -> set[str]:
    """Asked of the SQLAlchemy metadata rather than by grepping for `__tablename__`, so a
    table declared through a mixin or a loop is still counted."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.models import Base  # noqa: PLC0415
    return set(Base.metadata.tables)


# ---- what a document mentions -----------------------------------------------------------

def mentioned_routes(text: str) -> set[str]:
    return {re.sub(r"\{[^}]+\}", "{}", m.rstrip("/"))
            for m in re.findall(r"`(/[A-Za-z0-9_\-/{}]+)`", text)}


def mentioned_settings(text: str) -> set[str]:
    return set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", text)) | \
           set(re.findall(r"^\|\s*`?([A-Z][A-Z0-9_]{2,})`?\s*\|", text, re.M))


def mentioned_tables(text: str) -> set[str]:
    return set(re.findall(r"`([a-z][a-z0-9_]{2,})`", text)) | \
           set(re.findall(r"^\|\s*`?([a-z][a-z0-9_]{2,})`?\s*\|", text, re.M))


KINDS = {
    "routes": (live_routes, mentioned_routes),
    "settings": (live_settings, mentioned_settings),
    "tables": (live_tables, mentioned_tables),
}


def gaps() -> dict[str, list[str]]:
    """Per kind: what the app has that its reference document never names."""
    out = {}
    for kind, (live, mentions) in KINDS.items():
        have = live()
        named = mentions(DOCS[kind].read_text(encoding="utf-8"))
        out[kind] = sorted(have - named - set(PERMANENT[kind]))
    return out


def main() -> int:
    current = gaps()
    if "--write" in sys.argv:
        BASELINE.write_text(json.dumps({
            "_comment": "Known documentation gaps (GRPH-475). This list may only SHRINK — "
                        "test_docs_completeness.py fails on a new entry, and fails again if "
                        "an entry here has since been documented and not removed.",
            "gaps": current,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE.relative_to(ROOT)}")
    for kind, missing in current.items():
        total = len(KINDS[kind][0]())
        print(f"{kind:9s} {total - len(missing):3d}/{total:3d} documented, {len(missing)} missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
