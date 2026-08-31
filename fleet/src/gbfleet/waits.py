"""Typed human waits on this tracker, not a second board (P30 D11).

Finder for `until`: blocked items tagged `wait:merge` (or decision / secret /
access / deploy). Free-text `blocker` without a type is not a wait. The
supervisor does not call this — search_items is not in ALLOWED_TOOLS.
"""

from __future__ import annotations

from typing import Any

WAIT_KINDS = ("merge", "decision", "secret", "access", "deploy")
WAIT_TAGS = tuple(f"wait:{kind}" for kind in WAIT_KINDS)


def ids(client: Any, *, project_id: str | None = None) -> list[str]:
    """Wait-tagged blocked item ids, first-seen order. Empty means none, not unknown."""
    found: list[str] = []
    seen: set[str] = set()
    for tag in WAIT_TAGS:
        arguments: dict[str, Any] = {"tags": [tag], "status": "blocked"}
        if project_id:
            arguments["project_id"] = project_id
        payload = client.call("search_items", **arguments)
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get("id")
            if key and key not in seen:
                seen.add(str(key))
                found.append(str(key))
    return found
