"""Keep a bound child's lease alive while its process is (GRPH-1116).

The child cannot heartbeat during a long foreground command, and the supervisor
may not call the worker `heartbeat` tool (`ALLOWED_TOOLS` stays the two reads).
This posts `kind=lease` on the observations route the wave key already has.

A post names the items the roster last said this child holds. `ever_held` is the
wrong list: a review handoff stays on it after the build lease has moved on, and
keeping that lease alive would hold somebody else's item. Once `process.poll()`
is not None the posts stop, and the server clock lapses the lease on its own.
"""

from __future__ import annotations

import time

from . import observe
from . import worktree as wt_mod

#: Until a roster has said. Same idea as gbagent's fallback: short enough that one
#: missed post is still inside the presence TTL (three intervals), and not so short
#: that a tick posts on every poll.
FALLBACK_INTERVAL = 30.0


def _same_commit(a: str, b: str) -> bool:
    """Prefix match, with a floor. Git abbreviates; a 4-character match is a collision."""
    x = (a or "").strip().lower()
    y = (b or "").strip().lower()
    if len(x) < 7 or len(y) < 7:
        return bool(x) and x == y
    return x.startswith(y) or y.startswith(x)


def branch_commit(child) -> str | None:
    """HEAD when it is past the cut base. None when unread, empty, or still the base.

    An unread ref does not stamp authorship. The lease is still extended: a git
    failure must not look like the process died.
    """
    worktree = getattr(child, "worktree", None)
    if worktree is None:
        return None
    try:
        head = wt_mod._git(worktree, "rev-parse", "HEAD").strip()
    except Exception:  # noqa: BLE001 — unread is not a commit
        return None
    if not head:
        return None
    base = (getattr(child, "base", "") or "").strip()
    if base and _same_commit(head, base):
        return None
    return head


def _interval(wave) -> float:
    part = getattr(wave, "partition", None)
    raw = getattr(part, "heartbeat_interval", None) if part is not None else None
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return FALLBACK_INTERVAL


def keep(wave, children, client, *, now: float | None = None) -> None:
    """Post `kind=lease` for each running child that holds an item and is due.

    Failures land on `wave.failures` and are not fatal — the same bargain as
    `_observe`. A refusal is not retried on the next 1s poll: the clock still
    advances, so a standing 422 is one line per interval rather than one per tick.
    `extended: false` is a normal reply (the lease has moved) and is not a failure.
    """
    if client is None or wave is None:
        return
    moment = time.monotonic() if now is None else now
    interval = _interval(wave)
    for child in children:
        if not getattr(child, "running", False):
            continue
        agent_id = getattr(child, "agent_id", None)
        if not agent_id:
            continue
        held = [i for i in (getattr(child, "held_items", None) or []) if i]
        if not held:
            # A bound child's item is known from its seat before any roster reading lists
            # the hold (GRPH-1349); without this a child busy from the start is never kept.
            got = getattr(child, "assigned", None)
            if isinstance(got, dict) and got.get("state") == "claimed" and got.get("item"):
                held = [str(got["item"])]
        if not held:
            continue
        last = float(getattr(child, "lease_kept_at", 0.0) or 0.0)
        if last > 0 and (moment - last) < interval:
            continue
        commit = branch_commit(child)
        for item_id in held:
            payload = {
                "item_id": item_id,
                "kind": "lease",
                "branch": child.branch,
                "agent_id": agent_id,
            }
            if commit:
                payload["commit"] = commit
            try:
                reply = client.post_observation(**payload)
                if (isinstance(reply, dict) and reply.get("extended") is False
                        and _still_in_progress(client, item_id)):
                    _note_lapsed_live(wave, child, item_id)
            except Exception as exc:  # noqa: BLE001 — a wave is not broken by a missed beat
                wave.observations_refused += 1
                wave.failures.append(f"{item_id}: lease not extended ({exc})")
        child.lease_kept_at = moment


def _still_in_progress(client, item_id: str) -> bool:
    """GRPH-1387. `extended: false` is a lapse only while the item is still `in_progress`.

    A builder that has just moved its item to review, done or released has no build lease
    to extend; that is a hand-off, not a lapse. When the status cannot be read, nothing is
    recorded: a false "lapsed" line makes a healthy wave look stuck.
    """
    try:
        got = client.call("get_item_details", id=item_id)
    except Exception:  # noqa: BLE001 — an unreadable status is not a lapse
        return False
    return isinstance(got, dict) and got.get("status") == "in_progress"


def _note_lapsed_live(wave, child, item_id: str) -> None:
    """The server would not extend a lease whose process is alive: say so once per item."""
    lines = getattr(wave, "lease_lapsed_live", None)
    if lines is None:
        return
    if any(line.startswith(f"{item_id}:") for line in lines):
        return
    pid = getattr(getattr(child, "process", None), "pid", None)
    lines.append(f"{item_id}: lease not extended for live child pid {pid} ({child.agent_id})")
    observe.emit("lease_lapsed_live_child", item=item_id, pid=pid, agent=child.agent_id)
