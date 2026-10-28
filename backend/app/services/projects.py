"""Project resolution helpers — shared by MCP and public endpoints.

After the seeded ``core`` project went away, no write may assume a fixed project
id. These helpers pick a sensible project so single-project deploys "just work"
while multi-project callers can still be explicit.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app import tagging
from app.models import (
    Agent,
    Event,
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

    PRD-43 D8: assigns a random public_path_id on creation. Free keeps it random;
    pro/team/enterprise may claim a custom one.
    """
    from app.services.platform import random_slug, get_config

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
    # PRD-43 D8: assign random public_path_id via platform_config.
    cfg = get_config(db, project.id)
    cfg.public_path_id = random_slug()
    db.add(Membership(user_id=owner_user_id, project_id=project.id, role="owner", access="write"))
    db.commit()
    db.refresh(project)

    # PRD-43 D4: inherit org feedback defaults so new projects match the org posture.
    if org_id:
        from app.models import Organization
        from app.services.platform import get_config
        org = db.get(Organization, org_id)
        if org is not None and org.feedback_default_on:
            cfg = get_config(db, project.id)
            cfg.intake_enabled = True
            cfg.public_form_enabled = True
            cfg.public_share_enabled = True
            db.commit()

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
    """Return ``project_id`` if it names an existing project, the default if it was
    OMITTED, and ``None`` if it was named and does not exist.

    A named-but-existing project is returned as-is; authorization is the caller's
    job (``require_readable``/``require_writable``) so a named-but-forbidden id is
    rejected there, not silently swapped. Only the *fallback* is bounded by
    ``allowed_ids`` — the caller's own projects — so an omitted id never resolves to
    another tenant's first-by-name project (AL-71).

    **A named-but-nonexistent id is not a fallback** (GRPH-427). It used to be, and the
    result was that ``?project_id=does-not-exist`` returned HTTP 200 carrying the caller's
    default project's health — an authoritative-looking answer about unrelated data, with
    nothing to tell the caller a substitution had happened. The caller said which project
    they meant; a typo must not become a confident answer about another one.

    ``None`` rather than an exception, because every caller already treats it as not-found
    and does so with the right status: ``require_readable`` documents that it fails closed
    on ``None``, the key path's ``pid not in readable`` catches it, and the public route
    falls through to its 404. Raising here would have to be caught in five places, one of
    which (``routers/public``) has no handler and would have turned a typo into a 500.

    Empty string counts as OMITTED, not named: ``?project_id=`` is how a client with no
    project selected actually calls these routes, and 404-ing it would break the commonest
    request in the product."""
    if not project_id:
        return default_project_id(db, allowed_ids)
    return project_id if db.get(Project, project_id) is not None else None


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
#
# `triage_new` and `lessons_published` are the Project Home Jump-to populations
# (GRPH-1064). They are one COUNT each, over the predicates the Triage and Lessons
# views already use (`requests.untriaged_clause`, `memory.published_lessons_where`).
# A second copy would drift, and loading the rows to call len() is the cost GRPH-431
# removed. The statement count does not grow with the queue.
def shell_counts(db: Session, project_id: str | None) -> dict:
    """The integers the shell draws, without the collections it used to draw them from."""
    from app.services import memory as mem_svc
    from app.services import requests as req_svc

    if not project_id:
        # No project resolved means no scope. Zeroes, not the whole instance — asking bare
        # is how a nav badge ends up counting every project on the box.
        return {
            "items": 0, "items_in_progress": 0, "requests": 0, "review": 0,
            "triage_new": 0, "lessons_published": 0,
        }

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
        "triage_new": req_svc.untriaged_count(db, project_id),
        "lessons_published": mem_svc.count_published_lessons(db, project_id),
    }


# ---- release ref (GRPH-1109 / PRD-P57 S2) ----------------------------------------------------

RELEASE_REF_LITERAL = "release_ref stores one literal ref, not a pattern"


def normalize_release_ref(raw: str | None) -> str | None:
    """`None` or blank clears; anything else must be ONE literal branch name.

    Globs are refused with the same metacharacters gitops refuses on its branch literals
    (`gitops.GLOB_METACHARS`), and so are naming tokens and whitespace: this column stores
    the one ref the merge rule watches, so `release/*` has no single answer to give.
    """
    from app.services.gitops import GLOB_METACHARS

    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if any(ch in value for ch in GLOB_METACHARS) or "{" in value or "}" in value:
        raise ValueError(f"{RELEASE_REF_LITERAL}: {value!r} is a pattern")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{RELEASE_REF_LITERAL}: {value!r} contains whitespace")
    return value


def set_release_ref(db: Session, project: Project, raw: str | None) -> str | None:
    """Store (or clear) the project's release ref. Caller commits.

    No linked-box check, on purpose: the gitops PATCH refusal on a linked box guards the
    org's delivery contract, and this is a fact about this box's own repository.
    """
    project.release_ref = normalize_release_ref(raw)
    return project.release_ref


def release_ref_view(db: Session, project_id: str | None) -> dict:
    """`{ref, source}` for a reader that must not mistake unset for `main`.

    `source` is `project` when set and `unmeasured` when not — the gitops word for the
    same state, so a supervisor never has to treat a bare null as an answer. A missing
    project is `unknown`: nobody could look, which is not the same as nothing set.
    """
    project = db.get(Project, project_id) if project_id else None
    if project is None:
        return {"ref": None, "source": "unknown"}
    if project.release_ref:
        return {"ref": project.release_ref, "source": "project"}
    return {"ref": None, "source": "unmeasured"}


# ---- switcher summaries (GRPH-1131 / PRD-P59 D3) -------------------------------------------
#
# The project switcher draws `24 items · 3 agents · main` and a `2 live` chip on every row,
# or `idle 12d` when nothing has happened. `ProjectOut` carries none of those numbers, and
# the obvious way to get them — fetch each project's items and agents, call `len()` — is
# GRPH-431 again on the one surface that renders on EVERY route: a cost that grows with the
# biggest project the caller can read, paid to draw a navigation affordance.
#
# So every number here is a grouped aggregate over a bounded id list. The statement count is
# constant in the number of PROJECTS and independent of the number of items, agents and
# ledger rows; `test_project_summaries.py` pins that by adding rows and re-measuring.
#
# THREE definitions are borrowed rather than restated, because each already has one owner
# and a second copy is a second chance to drift:
#
#   live    — `fleet.presence_ttl_seconds()`, the same clock `_has_specialised_agents`
#             compares `last_seen_at` against in SQL. Not a constant here: a project that
#             raises its lease would otherwise get a switcher declaring its workers dead.
#   roster  — dismissed agents are out, which is what `live.board` counts as `total_agents`.
#   branch  — `gitops.base_branch_overlay`, the project-then-org overlay. Unset stays None.

#: The stored agent state that is NOT a fresh-heartbeat question. `presence_state` checks it
#: before the clock because a quarantined agent may still be heartbeating — that is what got
#: it quarantined — so `last_seen_at` alone would report a refused agent as fleet capacity.
#: The other member of `fleet._ABSENT`, `offline`, is derived from the clock and so is
#: expressed by the cutoff itself rather than by a stored value.
_NOT_LIVE_STATE = "quarantined"


def _aware(dt: datetime | None) -> datetime | None:
    """UTC-aware, whatever the dialect handed back. SQLite returns naive datetimes and
    Postgres returns aware ones, so a MAX() over a timestamp column is naive on one engine
    and aware on the other — comparing or serializing it without this differs by dialect."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _latest(*stamps: datetime | None) -> datetime | None:
    """The newest of several stored timestamps, tz-normalised, or None when all are None.

    BOTH sources, never one. `Item.updated_at` alone misses a project whose recent work was
    memory, PRD or request writes; `Event.ts` alone misses items moved by a path that
    recorded no ledger row. Either miss renders as `idle 12d` on a project somebody worked
    in yesterday, and idle is the reassuring wrong answer — the switcher is read at a
    glance to pick where to work next.
    """
    aware = [a for a in (_aware(s) for s in stamps) if a is not None]
    return max(aware) if aware else None


def project_summaries(db: Session, readable_ids: list[str]) -> list[dict]:
    """One row per readable project, as grouped aggregates. Ordered by project name.

    `readable_ids` is the caller's authz answer, computed at the boundary
    (`authz.readable_project_ids`) and passed in: services stay principal-free, and a count
    that resolved its own scope is how a nav badge ends up counting every project on the
    box — the failure `shell_counts` guards against for a single project. Nothing is ever
    counted across tenants, because nothing outside this list is queried at all.

    A project with nothing in it still gets a row. Dropping it would make the switcher lose
    a project the caller can read, which reads as "you have no access" rather than "it is
    empty" — the two are different facts and only one of them is true.
    """
    from app.services import fleet as fleet_svc
    from app.services import gitops

    if not readable_ids:
        return []
    projects = list(db.scalars(
        select(Project).where(Project.id.in_(readable_ids)).order_by(Project.name)).all())
    if not projects:
        return []
    ids = [p.id for p in projects]

    item_rows = {
        pid: (n, latest)
        for pid, n, latest in db.execute(
            select(Item.project_id, func.count(), func.max(Item.updated_at))
            .where(Item.project_id.in_(ids))
            .group_by(Item.project_id))
    }
    ledger = {
        pid: latest
        for pid, latest in db.execute(
            select(Event.project_id, func.max(Event.ts))
            .where(Event.project_id.in_(ids))
            .group_by(Event.project_id))
    }

    live_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=fleet_svc.presence_ttl_seconds())
    is_live = and_(Agent.state != _NOT_LIVE_STATE, Agent.last_seen_at >= live_cutoff)
    agent_rows = {
        pid: (n, live_n)
        for pid, n, live_n in db.execute(
            select(Agent.project_id, func.count(),
                   func.coalesce(func.sum(case((is_live, 1), else_=0)), 0))
            .where(Agent.project_id.in_(ids), Agent.dismissed_at.is_(None))
            .group_by(Agent.project_id))
    }
    branches = gitops.base_branch_overlay(db, projects)

    out = []
    for p in projects:
        item_count, item_latest = item_rows.get(p.id, (0, None))
        agent_count, live_count = agent_rows.get(p.id, (0, 0))
        out.append({
            "project_id": p.id,
            "item_count": int(item_count or 0),
            "agent_count": int(agent_count or 0),
            "live_agents": int(live_count or 0),
            "default_branch": branches.get(p.id),
            "last_activity_at": _latest(item_latest, ledger.get(p.id)),
        })
    return out
