"""Typed human waits on the same tracker (GRPH-612 / P30 D11).

A child that cannot proceed without a human does not invent a second queue.
It files a small item tagged `wait:merge` (or decision / secret / access /
deploy), blocks the original on that item, and leaves. Free-text `blocker`
without a type is not a wait — "please look" is stuck, not a human act.

When the wait item reaches `done`, dependents that were `blocked` return to
`next` unless another unmet dep remains. `in_progress` with a live lease is
not rewritten. Moving the original to `review` or `done` while a wait dep is
open is refused: filing a wait is not finishing the work.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Item

WAIT_KINDS = ("merge", "decision", "secret", "access", "deploy")
WAIT_TAGS = tuple(f"wait:{kind}" for kind in WAIT_KINDS)
_WAIT_TAGS_LOWER = {t.lower() for t in WAIT_TAGS}


def wait_tag(kind: str) -> str:
    cleaned = (kind or "").strip().lower()
    if cleaned.startswith("wait:"):
        cleaned = cleaned[5:]
    if cleaned not in WAIT_KINDS:
        raise ValueError(
            f"unknown wait type {kind!r}. Typed waits are {', '.join(WAIT_TAGS)}. "
            "Free-text is not a type."
        )
    return f"wait:{cleaned}"


def wait_tags_on(item: Item) -> list[str]:
    return [t for t in (item.tags or []) if str(t).lower() in _WAIT_TAGS_LOWER]


def is_human_wait(item: Item) -> bool:
    """A typed wait. `blocker="please look"` with no `wait:` tag is not one."""
    return item.status == "blocked" and bool(wait_tags_on(item))


def waiting(db: Session, project_id: str | None = None) -> list[Item]:
    """Blocked items carrying a `wait:` tag — the finder until/search_items use.

    Status+tag, not the free-text blocker. An empty list means no typed waits,
    not "nobody looked".
    """
    from app.services import items as items_svc

    return [it for it in items_svc.list_items(db, project_id=project_id) if is_human_wait(it)]


def unfinished_wait_deps(db: Session, item: Item) -> list[str]:
    """Wait-tagged items this one still depends on (not yet `done`)."""
    from app.services import prioritization as prio

    ctx = prio.context(db, item.project_id)
    out = []
    for dep_id in prio.blocked_by(ctx, item):
        dep = ctx.by_id.get(dep_id)
        if dep is not None and wait_tags_on(dep):
            out.append(dep.id)
    return out


def release_waiters(db: Session, wait: Item) -> list[str]:
    """After a wait item reaches `done`: blocked dependents with no remaining
    unmet deps return to `next`. Does not rewrite `in_progress`.
    """
    from app.services import prioritization as prio

    ctx = prio.context(db, wait.project_id)
    released: list[str] = []
    for dep_id in ctx.dependents.get(wait.id, []):
        dependent = ctx.by_id.get(dep_id)
        if dependent is None or dependent.status != "blocked":
            continue
        if prio.blocked_by(ctx, dependent):
            continue
        dependent.status = "next"
        dependent.blocker = ""
        dependent.claimed_by = None
        dependent.claimed_at = None
        dependent.assignee = ""
        released.append(dependent.id)
    if released:
        db.commit()
    return released
