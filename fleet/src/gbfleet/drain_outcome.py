"""Classify the end of a `--prd` cycle as drained, stalled or working (GRPH-P99 s1).

A pure function over the cycle's report fields and the PRD's open items, so it is
tested without a server. It decides nothing about notifying; it only names the state
and, for a stall, why each open item is not moving.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DRAINED = "drained"
STALLED = "stalled"
WORKING = "working"

#: Exits that mean the PRD has nothing left to do.
_DRAINED_REASONS = frozenset({"no-open-items", "prd-closed"})
#: Exits that are stalled outright. `review-needs-person` (GRPH-1830) waits on a person: no
#: further cycle can move it, so it is a stall to report, not work in flight.
_STALLED_REASONS = frozenset({"stalled", "review-unsigned", "review-needs-person", "stuck"})
#: The exit `until --prd` takes instead of `idle` while the PRD still has unfinished build
#: items (GRPH-1962). Unlike `idle`, a ready item does not make it `working`: the cycle
#: already looked and did not move it.
STUCK = "stuck"
#: The wait-line prefix `until.needs_person_line` writes (GRPH-1830).
NEEDS_PERSON = "review-needs-person: "
#: Exits that are stalled only when open build items remain.
_IDLE_REASONS = frozenset({"idle", "idle-with-waits"})


@dataclass(frozen=True)
class OpenItem:
    """One PRD item as the classifier needs it."""

    id: str
    status: str = "next"
    #: A walk or planner-only item: never a build item.
    walk: bool = False
    ready: bool = False
    #: Held by a live child (claimed, or its review is being read).
    live: bool = False
    #: The row's free-text `blocker` (GRPH-1945): what a person must do to clear it.
    blocker: str = ""
    #: Deliberately parked: a typed wait on a person (GRPH-1962). Not unfinished work.
    parked: bool = False
    #: What the server row says is missing for a build: `acceptance`, `touchpoints`.
    missing: tuple[str, ...] = ()
    #: Who holds it when `live` (claimed_by or review_claimed_by).
    held_by: str = ""


#: The most of an item's blocker a stall reason carries (GRPH-1945).
BLOCKER_LIMIT = 200


def _short_blocker(text: str) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= BLOCKER_LIMIT:
        return flat
    return flat[:BLOCKER_LIMIT - 1].rstrip() + "\u2026"


@dataclass(frozen=True)
class Outcome:
    state: str
    #: item id -> why it is not moving. Filled for `stalled` only.
    reasons: dict[str, str] = field(default_factory=dict)


def _build_open(items: list[OpenItem]) -> list[OpenItem]:
    return [i for i in items if i.status != "done" and not i.walk]


def _reason_for(item: OpenItem, held: list[dict], dependency_holds: list[dict],
                review_waits: list[str], refused: list[dict]) -> str:
    for row in refused:
        if row.get("item") == item.id:
            return f"delegate refused: {row.get('reason') or 'refused'}"
    for row in dependency_holds:
        if row.get("item") == item.id:
            return f"held on dependency: {row.get('waiting_on') or 'unknown'}"
    for row in held:
        if item.id in (row.get("items") or []):
            who = ", ".join(str(h) for h in row.get("held_by") or []) or "unknown"
            because = "; ".join(str(b) for b in row.get("because") or [])
            return f"held by {who}" + (f" ({because})" if because else "")
    for line in review_waits:
        if line.startswith(f"{NEEDS_PERSON}{item.id}:"):
            return "needs a person: " + line[len(f"{NEEDS_PERSON}{item.id}:"):].strip()
        if line.startswith(f"{item.id}:"):
            return line.split(":", 1)[1].strip()
    if item.status == "review":
        return "in review, no live reviewer"
    blocker = _short_blocker(item.blocker)
    if blocker:
        return f"not ready: {item.status}: {blocker}"
    if item.missing:
        return f"not ready: {item.status}: missing {', '.join(item.missing)}"
    if item.live:
        return f"held elsewhere: {item.status}, held by {item.held_by or 'unknown'}"
    if item.ready:
        return "ready, not claimed this cycle"
    return f"not ready: status {item.status}"


def unfinished(items: list[OpenItem], *, held: list[dict] | None = None,
               dependency_holds: list[dict] | None = None,
               review_waits: list[str] | None = None,
               delegate_refused: list[dict] | None = None) -> dict[str, str]:
    """Every build item of the PRD that is neither done nor parked, and why (GRPH-1962).

    Empty means `until --prd` may exit `idle`: each item is done or waits on a person.
    Anything else is `stuck`. Reasons are read from the server rows, the same text a
    notice carries, so the notice and the exit agree.
    """
    return {
        i.id: _reason_for(i, held or [], dependency_holds or [], review_waits or [],
                          delegate_refused or [])
        for i in _build_open(items) if not i.parked}


def classify(reason: str, items: list[OpenItem], *, spawned: int = 0,
             held: list[dict] | None = None,
             dependency_holds: list[dict] | None = None,
             review_waits: list[str] | None = None,
             delegate_refused: list[dict] | None = None) -> Outcome:
    """Classify one cycle. `items` is every item of the PRD, done ones included."""
    open_items = _build_open(items)
    if reason in _DRAINED_REASONS or not open_items:
        return Outcome(DRAINED)
    if spawned or any(i.live for i in open_items):
        return Outcome(WORKING)
    if reason in _STALLED_REASONS or reason in _IDLE_REASONS:
        if any(i.ready for i in open_items) and reason in _IDLE_REASONS \
                and not delegate_refused:
            return Outcome(WORKING)
        return Outcome(STALLED, {
            i.id: _reason_for(i, held or [], dependency_holds or [],
                              review_waits or [], delegate_refused or [])
            for i in open_items})
    return Outcome(WORKING)
