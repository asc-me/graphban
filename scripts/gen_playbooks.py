#!/usr/bin/env python3
"""The playbook pack manifest (GRPH-1491, PRD GRPH-P65).

One file, `backend/scripts/gban_playbooks.json`, holding every skill, loop and playbook body
with its sha256. The API image and the native release ship it (as `app/data/playbooks.json`)
and `GET /api/playbooks` serves it, stamped with the running `git_sha`. The container has no
checkout, so the bodies are baked at build time from the same sources `gen_subagents.py`
already copies. AGENTS.md and CLAUDE.md are deliberately not entries.

    python scripts/gen_playbooks.py            # write the manifest
    python scripts/gen_playbooks.py --check    # exit 1 if it is stale
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = REPO / "backend" / "scripts" / "gban_playbooks.json"
sys.path.insert(0, str(REPO / "scripts"))


def _entry(kind: str, path: str, body: str) -> dict:
    return {"path": path, "kind": kind,
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "body": body}


def build() -> list[dict]:
    """Entries sorted by path. A skill is one entry (path `.claude/skills/<slug>/SKILL.md`;
    the client also writes `.grok/skills` and `.agents/skills`). A loop is one entry per file
    `gen_subagents.py` renders for a role directory or the Cursor rule. A playbook is one entry
    per `scripts/plugin_skills/<slug>`."""
    import gen_subagents as gs

    entries: list[dict] = []
    files = gs.render_files()
    for rel, body in files.items():
        if rel.startswith(f"{gs.PLUGIN_DIR}/"):
            continue
        if rel.startswith(".claude/skills/") or rel.startswith(".agents/skills/"):
            continue
        if "/agents/" in rel or rel.startswith(".cursor/rules/"):
            entries.append(_entry("loop", rel, body))
    for skill in sorted(gs.SKILLS_SRC.glob("*/SKILL.md")):
        entries.append(_entry("skill", f".claude/skills/{skill.parent.name}/SKILL.md",
                              skill.read_text(encoding="utf-8")))
    for rel, body in files.items():
        if rel.startswith(f"{gs.PLUGIN_DIR}/skills/"):
            entries.append(_entry("playbook", rel, body))
    if not any(e["kind"] == "skill" for e in entries) or \
            not any(e["kind"] == "playbook" for e in entries):
        raise SystemExit("playbook pack has no skills or no playbooks — a source moved")
    entries.sort(key=lambda e: e["path"])
    return entries


def render() -> str:
    return json.dumps({"files": build()}, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        if not MANIFEST.is_file() or MANIFEST.read_text(encoding="utf-8") != text:
            print("playbook pack is stale — run `python scripts/gen_playbooks.py`")
            return 1
        print("playbook pack up to date")
        return 0
    MANIFEST.write_text(text, encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
