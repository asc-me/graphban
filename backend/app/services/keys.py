"""Resolve a human- or agent-supplied key to the stored id it means (PRD-13 / AL-257).

The inverse of ``tagging.render``. A key like ``GRPH-12`` is a *rendering* of
``(project tag, kind, number)`` and is not stored anywhere; the stored id is frozen at
issue time. So every path that accepts an id from outside has to translate, and it has
to keep translating keys that were rendered under a tag the project no longer holds —
from git history, merged PR titles, frozen PRD snapshots, and agent memory.

**Reads and writes both.** ``claim_next``, ``heartbeat``, ``update_item``, and
``release_item`` resolve through here exactly like ``get_item_details`` does. An agent
that claimed a lease on ``AL-12`` keeps working after its project is retagged only
because the write path translates too — and a missed call site fails *only* on a
renamed project, which no fresh-instance test would ever notice.

Resolution order, first hit wins:

1. **Current form** — parse the grammar, find the project holding that tag, look the
   entity up by number. This is what a key means *today*, so it outranks history.
2. **Tag history** — a tag the project used to hold. One row per rename.
3. **Legacy table** — an id issued before tags existed. ``R-`` and ``PRD-`` were
   entity-kind markers rather than project tags, so tag history cannot express them.
4. **Identity** — the stored id itself. Last, deliberately: once tags move, a stale
   stored id and a live rendered key can collide on the same string, and the live
   rendering has to win. This rung exists so internal callers and anything the first
   three cannot express still resolve.

Every rung is filtered by ``kind``, so asking for a PRD can never hand back an item
that happens to share a number.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import tagging
from app.models import Agent, Item, LegacyEntityKey, Prd, Project, ProjectTagHistory, Request

# kind -> model. The keys match tagging.KIND_LETTER so the grammar and the lookup
# can never disagree about what kinds exist.
MODELS = {"item": Item, "request": Request, "prd": Prd, "agent": Agent}


def resolve(db: Session, key: str, kind: str) -> str | None:
    """The stored id for ``key``, or None when it names nothing of that kind.

    None is the caller's 404 — it means "no such entity", not "malformed input".
    """
    model = MODELS[kind]
    key = (key or "").strip()
    if not key:
        return None

    parsed = tagging.parse(key)
    if parsed and parsed[1] == kind:
        tag, _kind, number = parsed

        project = db.scalar(select(Project).where(Project.tag == tag))
        if project is not None:
            found = db.scalar(
                select(model).where(model.project_id == project.id, model.number == number)
            )
            if found is not None:
                return found.id

        # A tag the project used to hold. Reuse is forbidden per deployment (AL-258),
        # which is what keeps this lookup unambiguous.
        history = db.get(ProjectTagHistory, tag)
        if history is not None:
            found = db.scalar(
                select(model).where(
                    model.project_id == history.project_id, model.number == number
                )
            )
            if found is not None:
                return found.id

    legacy = db.get(LegacyEntityKey, key)
    if legacy is not None and legacy.entity_type == kind:
        return legacy.entity_id

    return key if db.get(model, key) is not None else None


def mint(db: Session, project_id: str, kind: str) -> tuple[str, int]:
    """Reserve the next ``(stored_id, number)`` for a new entity in ``project_id``.

    The number is ``max(number) + 1`` **within the project, per kind** — replacing the
    single global counter that made every project's tickets share one sequence. Two
    projects can now both hold a number 235; the ``(project_id, number)`` unique
    constraint is what keeps that safe, and it is also the backstop if this path is ever
    bypassed.

    At issue time the stored id is simply the rendered key, so the two agree until the
    project is retagged. From then on they diverge permanently and only the rendering
    is user-facing — that divergence is the accepted cost of never rewriting an id.
    """
    model = MODELS[kind]

    _lock_project(db, project_id)

    project = db.get(Project, project_id)
    if project is None:
        raise ValueError(f"unknown project: {project_id!r}")

    number = (
        db.scalar(select(func.max(model.number)).where(model.project_id == project_id)) or 0
    ) + 1
    stored_id = tagging.render(project.tag, kind, number)

    # A tag can equal a prefix from the pre-tag era (this deployment has a project
    # tagged `AL`), and back then numbering was GLOBAL — so `AL-101` may already be
    # owned by a different project even though *this* project has no number 101. The
    # id is a primary key, so walk forward until it is free. Numbers only ever
    # increase, which keeps them unique within the project.
    while db.get(model, stored_id) is not None:
        number += 1
        stored_id = tagging.render(project.tag, kind, number)

    return stored_id, number


def _lock_project(db: Session, project_id: str) -> None:
    """Serialize minting for one project so two concurrent creates can't pick the same
    number. Scoped to the project, so unrelated projects never contend.

    SQLite has no ``SELECT … FOR UPDATE``; SQLAlchemy silently drops the clause there
    rather than failing, so the branch is explicit. SQLite already serializes writers at
    the database level, which is why nothing is needed — stating that beats leaving a
    portable-looking lock that quietly does nothing on one of the two engines we ship.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(select(Project.id).where(Project.id == project_id).with_for_update())


def resolve_item(db: Session, key: str) -> str | None:
    return resolve(db, key, "item")


def resolve_request(db: Session, key: str) -> str | None:
    return resolve(db, key, "request")


def resolve_prd(db: Session, key: str) -> str | None:
    return resolve(db, key, "prd")
