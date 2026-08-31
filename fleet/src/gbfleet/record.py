"""Write measured paths back onto an item. Not a supervisor job.

P30 D10. The supervisor measures (`touchpoints.measure`) and reports `touched` on the
child record. It does not patch the item: `ALLOWED_TOOLS` is two reads. Whoever has
standing — `gbagent` during the run, `until` (planner) after a reap — sends **this
reap's measured paths only** through `update_item`. The server unions, so the client
must not resend the prediction, and must not send `[]`. Empty is not "no collision".
"""

from __future__ import annotations

from typing import Any


def cleaned_paths(paths: list[str] | None) -> list[str]:
    """This reap's measured paths, blanks and duplicates dropped. Empty means do not write."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for path in paths or []:
        if not isinstance(path, str):
            continue
        p = path.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        cleaned.append(p)
    return cleaned


def measured(client: Any, item_id: str, paths: list[str] | None) -> dict | None:
    """Union this reap's measured paths onto the item. Empty is not a write.

    Returns the tool result, or `None` when there is nothing honest to send — no item,
    or no paths. Callers must not treat `None` as "the item collides with nothing".
    """
    cleaned = cleaned_paths(paths)
    if not cleaned or not item_id:
        return None
    return client.call("update_item", id=item_id, touchpoints=cleaned)
