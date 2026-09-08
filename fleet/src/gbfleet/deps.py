"""Is an item's finished work actually in the base its child will branch from? (GRPH-798)

Reported from super-arc: SA-417 was signed off with a full attestation, and its commit existed
only on `origin/gb/p11-m1-4`. Children branch from the remote default. So a dependent item was
green-lit by the tracker while its dependency was absent from the base it would be built on —
SA-420 would have had to re-invent DeviceToken.

**`done` means attested, not merged, and that is the design.** An attestation binds to a
COMMIT; nothing in the ledger claims that commit went anywhere. `blocked_by` lists *unfinished*
dependencies, so a done-but-unmerged one is invisible there by construction — it is finished.

This detects rather than decides. The server cannot: it holds an item id and a commit, and has
no repository to resolve them against. `gbfleet` does, so the check lives here — which means it
guards waves and not a human calling `delegate` by hand, and that limit is stated rather than
papered over.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .worktree import reaches


def _commits(item: dict) -> list[str]:
    """Every commit this item was attested at. The attestation carries it; `branch` often does
    not — a signed-off item's `branch` is routinely empty, so the commit is the fact to use."""
    out = []
    for e in item.get("evidence") or []:
        if not isinstance(e, dict):
            continue
        c = str(e.get("commit") or "")
        if c and c not in out:
            out.append(c)
    return out


def _is_dependency(row: dict) -> bool:
    return "dependency" in [str(t) for t in (row.get("link_types") or [])]


def check(planner: Any, item_id: str, repo: Path | str, base: str) -> tuple[list[dict], list[dict]]:
    """`(absent, unknown)` — finished dependencies whose work is not in `base`, and the ones
    this clone could not resolve.

    The two lists are separate deliberately. "Its commit is provably not in the base" is
    grounds to hold the item back; "I have never seen that commit" is not, and merging the two
    would either refuse every wave on a fresh clone or wave through the exact case this exists
    to catch, depending which way the collapse went.
    """
    absent: list[dict] = []
    unknown: list[dict] = []
    if not base:
        return absent, unknown
    try:
        related = planner.call("related_work", id=item_id) or {}
    except Exception:  # noqa: BLE001 — a failed lookup is not evidence of a missing dependency
        return absent, unknown
    for row in related.get("results") or []:
        if not isinstance(row, dict) or not _is_dependency(row) or row.get("status") != "done":
            continue
        commits = _commits(row)
        if not commits:
            # Done with no attested commit at all. Not this check's business — the completion
            # gate is what has an opinion about that — and inventing one here would report a
            # missing dependency for an item that may be perfectly merged.
            continue
        answers = [reaches(repo, base, c) for c in commits]
        if any(a is True for a in answers):
            continue
        entry = {"id": str(row.get("id") or ""), "title": str(row.get("title") or ""),
                 "commits": commits}
        (absent if any(a is False for a in answers) else unknown).append(entry)
    return absent, unknown


def explain(item_id: str, absent: list[dict], base: str) -> str:
    """One line a person can act on: what is missing, and that the remedy is a merge."""
    who = ", ".join(f"{d['id']} ({d['commits'][0][:9]})" for d in absent)
    return (f"{item_id} depends on finished work that is not in {base}: {who}. "
            f"A child would branch from {base} and not have it — merge first, or delegate "
            f"{item_id} by hand if you know better")
