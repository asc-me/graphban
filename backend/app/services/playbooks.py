"""The playbook pack the deployment publishes (GRPH-1491, PRD GRPH-P65).

The bodies are baked at build time into `app/data/playbooks.json` (image and native release);
a dev checkout reads the checked-in `backend/scripts/gban_playbooks.json`. The route stamps
the running `git_sha` on it — the same value `/health` reports.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.config import settings
from app.version import __version__

PACK_FILE = Path(__file__).resolve().parents[1] / "data" / "playbooks.json"
_DEV_FILE = Path(__file__).resolve().parents[2] / "scripts" / "gban_playbooks.json"


class PackUnavailable(RuntimeError):
    """The pack is missing or a body does not hash to its sha256 — serve nothing."""


def load_files(path: Path | None = None) -> list[dict]:
    src = path or (PACK_FILE if PACK_FILE.is_file() else _DEV_FILE)
    try:
        files = json.loads(src.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError) as exc:
        raise PackUnavailable(f"playbook pack unreadable: {src}") from exc
    for f in files:
        if hashlib.sha256(f["body"].encode("utf-8")).hexdigest() != f["sha256"]:
            raise PackUnavailable(f"playbook pack entry {f['path']} does not match its sha256")
        if f["path"] in ("AGENTS.md", "CLAUDE.md"):
            raise PackUnavailable(f"{f['path']} is not a pack entry")
    return files


def manifest(path: Path | None = None) -> dict:
    return {"version": __version__, "git_sha": settings.resolved_git_sha,
            "files": load_files(path)}
