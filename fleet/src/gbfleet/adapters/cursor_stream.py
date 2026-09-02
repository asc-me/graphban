"""Parse cursor-agent `--output-format stream-json` for files this run wrote (GRPH-215).

Isolated on purpose: the NDJSON shape must not leak past this module (AL-201). Reads
are not writes. Malformed lines are skipped — a mixed log must not fail the reap.

Empty is "the stream named no writes", not "safe to send [] to update_item".
`gbfleet.record.measured` already refuses an empty list.
"""
from __future__ import annotations

import json

from gbfleet.worktree import is_seat_relative

#: Documented write-like tool objects. `readToolCall` is deliberately absent.
WRITE_KEYS = ("writeToolCall", "editToolCall", "deleteToolCall")


def touched(text: str, *, cwd: str = "") -> list[str]:
    """Repo-relative paths this stream says the run wrote, in first-seen order."""
    found: list[str] = []
    seen: set[str] = set()
    stream_cwd = _norm(cwd)
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            raw = event.get("cwd")
            if isinstance(raw, str) and raw.strip():
                stream_cwd = _norm(raw)
            continue
        if event.get("type") != "tool_call":
            continue
        blob = event.get("tool_call")
        if not isinstance(blob, dict):
            continue
        for key, payload in blob.items():
            if key not in WRITE_KEYS:
                continue
            rel = _relative(_path_from(payload), stream_cwd)
            if not rel or rel in seen or is_seat_relative(rel):
                continue
            seen.add(rel)
            found.append(rel)
    return found


def _path_from(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    # Prefer the relative arg over the completed absolute path.
    result = payload.get("result")
    success = result.get("success") if isinstance(result, dict) else None
    for blob in (payload.get("args"), success):
        if isinstance(blob, dict):
            path = blob.get("path")
            if isinstance(path, str) and path.strip():
                return path.strip()
    return ""


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


def _relative(path: str, cwd: str) -> str:
    path = _norm(path)
    if not path:
        return ""
    if cwd:
        if path == cwd:
            return ""
        prefix = cwd + "/"
        if path.startswith(prefix):
            path = path[len(prefix):]
    if path.startswith("/"):
        # Absolute and not under cwd — not an honest repo-relative touchpoint.
        return ""
    return path
