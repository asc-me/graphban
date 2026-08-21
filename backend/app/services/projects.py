"""Project resolution helpers — shared by MCP and public endpoints.

After the seeded ``core`` project went away, no write may assume a fixed project
id. These helpers pick a sensible project so single-project deploys "just work"
while multi-project callers can still be explicit.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import tagging
from app.models import (
    Item,
    LegacyEntityKey,
    Membership,
    Project,
    ProjectTagHistory,
    Request,
    utcnow,
)


def unique_slug(db: Session, name: str) -> str:
    """A free project id derived from the name. Ids are frozen at creation and every
    entity key renders from the project's TAG, not this — so a suffixed slug is only
    ever cosmetic (PRD-13)."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "project"
    slug, n = base, 2
    while db.get(Project, slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def create_project(
    db: Session,
    *,
    name: str,
    owner_user_id: str,
    tag: str | None = None,
    accent: str = "",
    description: str = "",
    org_id: str | None = None,
) -> Project:
    """Create a project and make `owner_user_id` its owner.

    One code path for all three callers — the REST router, first-run provisioning
    (AL-283), and agent-side creation (AL-284) — because a project that skipped the
    owner Membership would be invisible to its own creator, and three implementations
    is three chances to skip it.

    An explicit tag is validated and refused on conflict; an omitted one is derived.
    Deriving rather than rejecting matters for bootstrapping: creating a project must
    not fail over a missing four-character string, and the result is visible and
    changeable immediately (PRD-13).
    """
    name = name.strip()
    if not name:
        raise ValueError("project name is required")
    if tag:
        available, reason = tag_available(db, tag)
        if not available:
            raise ValueError(f"tag {tag!r} is not available: {reason}")
        resolved_tag = tagging.normalize(tag)
    else:
        resolved_tag = unique_tag(db, name)

    project = Project(
        id=unique_slug(db, name),
        tag=resolved_tag,
        name=name,
        accent=accent or "#c6f24e",
        description=description or "",
        org_id=org_id,
    )
    db.add(project)
    db.flush()
    db.add(Membership(user_id=owner_user_id, project_id=project.id, role="owner", access="write"))
    db.commit()
    db.refresh(project)
    return project


def tag_available(db: Session, tag: str) -> tuple[bool, str]:
    """Is ``tag`` free on THIS deployment? Returns ``(available, reason)``.

    Three instance-local conditions make a tag unavailable — no reserved-word list ships
    in the product, so a fresh self-host starts with the whole namespace open, including
    ``AL`` and ``PRD``:

    1. **currently held** by a project
    2. **previously held** — in tag history. Reuse would make a key rendered under the
       old tag ambiguous the moment the new holder had an entity with the same number.
    3. **present as a pre-tag prefix** in the legacy table. This is the one that is easy
       to miss: ``PRD`` was never a project *tag*, so history cannot express it, but
       ``PRD-12`` is a legal rendering of item 12 in a project tagged ``PRD``. Letting a
       project claim it would collide with a legacy id that must resolve forever.

    ``R`` excludes itself by failing the two-character minimum.
    """
    try:
        tag = tagging.validate(tag)
    except ValueError as e:
        return False, str(e)

    if db.scalar(select(Project).where(Project.tag == tag)) is not None:
        return False, "already in use by another project"
    if db.get(ProjectTagHistory, tag) is not None:
        return False, "previously used on this deployment; tags are never reused"
    if db.scalar(select(LegacyEntityKey).where(LegacyEntityKey.old_key.like(f"{tag}-%"))):
        return False, "reserved by ids issued before project tags existed"
    return True, ""


def unique_tag(db: Session, name: str) -> str:
    """A free tag derived from ``name``, mirroring the ``_unique_slug`` convention.

    Derivation must always succeed rather than reject: every project needs a tag, and an
    agent bootstrapping one shouldn't fail over a missing four-character string. The
    result is visible and changeable immediately.
    """
    for candidate in tagging.variants(tagging.derive(name)):
        if tag_available(db, candidate)[0]:
            return candidate
    raise ValueError(f"could not derive a free tag for {name!r}")


def default_project_id(db: Session, allowed_ids: list[str] | None = None) -> str | None:
    """The first project by name, or None if the database has no projects yet.

    Pass ``allowed_ids`` (the caller's readable projects) so a single-project deploy
    "just works" without the fallback ever crossing into another tenant's project
    (AL-71). ``None`` means unscoped (legacy / trusted internal callers)."""
    stmt = select(Project).order_by(Project.name)
    if allowed_ids is not None:
        if not allowed_ids:
            return None
        stmt = stmt.where(Project.id.in_(allowed_ids))
    p = db.scalars(stmt).first()
    return p.id if p else None


def resolve_project_id(
    db: Session, project_id: str | None, allowed_ids: list[str] | None = None
) -> str | None:
    """Return ``project_id`` if it names an existing project, else the default.

    A named-but-existing project is returned as-is; authorization is the caller's
    job (``require_readable``/``require_writable``) so a named-but-forbidden id is
    rejected there, not silently swapped. Only the *fallback* is bounded by
    ``allowed_ids`` — the caller's own projects — so an omitted/unknown id never
    resolves to another tenant's first-by-name project (AL-71)."""
    if project_id and db.get(Project, project_id) is not None:
        return project_id
    return default_project_id(db, allowed_ids)


def retag_project(db: Session, project_id: str, new_tag: str) -> Project:
    """Move a project's tag. One UPDATE on one row, plus one history row (PRD-13).

    This is the operation the whole design exists to make cheap. Because keys are
    *rendered* rather than stored, nothing else in the database moves: not the twelve
    columns that hold an entity id, not the audit trail, not code-graph state already
    pushed to a cloud tenant, and not any in-flight agent claim. A test asserts exactly
    that — zero rows changed across all ten other tables.

    The history row and the tag move commit together. A tag that moved without its
    history row would silently break every key ever rendered under the old one, with no
    way to recover the mapping afterwards.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError(f"unknown project: {project_id!r}")

    tag = tagging.validate(new_tag)
    if tag == project.tag:
        return project  # a no-op must not write a history row and retire its own tag

    available, reason = tag_available(db, tag)
    if not available:
        raise ValueError(f"tag {tag!r} is not available: {reason}")

    # Chain the interval to the previous entry so history reads as a timeline rather
    # than a bag of retired tags.
    previous = db.scalars(
        select(ProjectTagHistory)
        .where(ProjectTagHistory.project_id == project_id)
        .order_by(ProjectTagHistory.held_until.desc())
    ).first()

    db.add(
        ProjectTagHistory(
            tag=project.tag,
            project_id=project_id,
            held_from=previous.held_until if previous is not None else None,
            held_until=utcnow(),
        )
    )
    project.tag = tag
    db.commit()
    db.refresh(project)
    return project


# What the app shell renders as badge numbers. The nav wanted four integers and was fetching
# four full collections to call `.length` on them (GRPH-431): 765 KB of items, 740 KB of memory
# shards and 621 KB of candidates on EVERY route, to draw three badges and a stat. The page
# whose data is 2.8 KB was moving 2.1 MB, and nginx logged three "upstream response is buffered
# to a temporary file" warnings serving one view of it.
#
# TWO STRATEGIES HERE ON PURPOSE, and the split is the interesting part.
#
# `items` and `requests` are counted in SQL, because `items.list_items` and
# `requests.list_requests` are pure `select` — no Python-side filtering — so `count(*)` over the
# same predicate is provably the same number.
#
# `review` is NOT counted in SQL. `memory.list_shards` drops expired candidates in Python
# (`age_state`) and may fold in global shards depending on the project, so a hand-written
# `count(*)` would be a SECOND definition of the review queue and would quietly disagree with
# the list it labels. A badge that says 4 above a list of 7 is worse than a slow badge. So the
# service is reused and its result counted: same rows loaded server-side as before, but they
# stop crossing the wire, which is the cost that was actually being paid.
#
# Making `review` a SQL count later is a real optimization, and its precondition is stated so
# nobody does it by eye: move the expiry rule and the global-shard inclusion into the query
# first, and keep `test_counts_match_the_collections_they_replace` as the pin.
def shell_counts(db: Session, project_id: str | None) -> dict:
    """The integers the shell draws, without the collections it used to draw them from."""
    from app.services import memory as mem_svc

    if not project_id:
        # No project resolved means no scope. Zeroes, not the whole instance — asking bare
        # is how a nav badge ends up counting every project on the box.
        return {"items": 0, "items_in_progress": 0, "requests": 0, "review": 0}

    def _count(model, **where) -> int:
        stmt = select(func.count()).select_from(model).where(model.project_id == project_id)
        for col, val in where.items():
            stmt = stmt.where(getattr(model, col) == val)
        return db.scalar(stmt) or 0

    # The reviewer's real backlog is candidates PLUS anything auto-published without them
    # (AL-287) — counting only candidates reads as "no work" on a project whose agents publish
    # directly, which is exactly when there is most to look at. Mirrors the nav's own filter.
    candidates = mem_svc.list_shards(db, project_id=project_id, status="candidate")
    auto = mem_svc.auto_triaged_shards(db, project_id=project_id)
    review = len(candidates) + sum(1 for s in auto if s.scoring_source in ("trusted", "agent"))

    return {
        "items": _count(Item),
        "items_in_progress": _count(Item, status="in_progress"),
        "requests": _count(Request),
        "review": review,
    }
