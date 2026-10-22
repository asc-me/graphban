"""Typed-link service (minimal — the Links graph UI lands in Phase 4)."""
from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models import Item, Link, Project
from app.security.authz import Forbidden
from app.services import keys

LINK_TYPES = ["dependency", "code", "semantic", "tag"]


def create_link(
    db: Session, *, a: str, b: str, type_: str = "dependency", reason: str = "",
    confidence: float = 1.0, project_id: str = "core",
) -> Link:
    if type_ not in LINK_TYPES:
        raise ValueError(f"invalid link type: {type_}")
    link = Link(project_id=project_id, a=a, b=b, type=type_, reason=reason, confidence=confidence)
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def delete_link(
    db: Session, *, a: str, b: str, type_: str | None = None, project_id: str | None = None,
) -> int:
    """Remove the directed (a, b) link(s) — the inverse of create_link. Omit `type_` to
    remove every link type for that pair; scope by `project_id` when given. Idempotent:
    returns the number of links removed (0 if the link never existed)."""
    stmt = delete(Link).where(Link.a == a, Link.b == b)
    if type_ is not None:
        stmt = stmt.where(Link.type == type_)
    if project_id is not None:
        stmt = stmt.where(Link.project_id == project_id)
    removed = db.execute(stmt).rowcount or 0
    db.commit()
    return removed


def list_links(db: Session, project_id: str | None = None) -> list[Link]:
    """Links touching `project_id` — by either ENDPOINT, not by the row's own project, so a
    cross-project edge shows on both sides (GRPH-1063). Request-endpoint links still match on
    the row's project, which is where they were always written."""
    stmt = select(Link)
    if project_id:
        mine = select(Item.id).where(Item.project_id == project_id)
        stmt = stmt.where(or_(Link.project_id == project_id, Link.a.in_(mine), Link.b.in_(mine)))
    return list(db.scalars(stmt.order_by(Link.id)).all())


def link_endpoints(
    db: Session, a: str, b: str, *, readable: list[str], writable: list[str],
) -> tuple[Item, Item]:
    """The scope rule for writing or removing an item link (GRPH-1063). Returns both items.

    A link joins two items the caller can READ, in projects of ONE organization; the source
    `a` (the dependent, for a dependency) must also be writable, since the link is stored in
    its project. Projects with no org (self-host, unlinked) are one org of their own, so any
    two readable ones may link. A linked box never reaches here: `link_items`/`unlink_items`
    are proxied to the cloud, which applies this same rule inside the org.

    Cross-org is REFUSED, not filtered, and the refusal is identical whether the far item is in
    another org, unreadable, or does not exist — anything else tells a caller which ids are
    real in a tenant they are not in. That is also why a missing id is not a `not_found` here.
    """
    pair = (a, b)
    found = [db.get(Item, keys.resolve_item(db, ref) or ref) for ref in pair]
    for ref, item in zip(pair, found):
        if item is None or item.project_id not in readable:
            raise _refused(a, b, ref)
    ia, ib = found
    if _org_of(db, ia.project_id) != _org_of(db, ib.project_id):
        raise _refused(a, b, b)
    if ia.project_id not in writable:
        raise Forbidden(
            f"item {a!r} is outside this key's write scope; a link is stored with its source",
            hint="link from an item you can write, or use a key with write access to it",
        )
    return ia, ib


def _org_of(db: Session, project_id: str) -> str | None:
    return db.scalar(select(Project.org_id).where(Project.id == project_id))


def _refused(a: str, b: str, ref: str) -> Forbidden:
    return Forbidden(
        f"cannot link {a!r} and {b!r}: an item link joins two items you can read, inside one "
        f"organization, and {ref!r} is not one — it does not exist, you cannot read it, or it is "
        "in another organization (cross-org links are always refused)",
        hint="use search_items to find an item you can read in this organization",
    )
