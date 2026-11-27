"""Typed-link service (minimal — the Links graph UI lands in Phase 4)."""
from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models import Item, Link, Project
from app.security.authz import Forbidden
from app.services import keys

LINK_TYPES = ["dependency", "code", "semantic", "tag", "found_during"]


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


# ---- PRD links (GRPH-P104 S1) -------------------------------------------------------------
# Two PRDs in different projects of one org, joined by request and accept. The link's messages
# are the pair's one thread (services/threads.py).

import uuid
from dataclasses import dataclass, field

from app.models import Organization, Prd, PrdLink, utcnow

PRD_LINK_STATUSES = ("pending", "accepted", "declined", "unlinked")


@dataclass
class Caller:
    """Who is acting, as the boundary resolved it. Services stay principal-free, so the router
    or MCP handler builds this from the key: its readable/writable projects, whether it is an
    agent, and the `label` / `owner` a message is signed with (never an email or seat id)."""
    readable: list[str] = field(default_factory=list)
    writable: list[str] = field(default_factory=list)
    is_agent: bool = False
    label: str = ""
    owner: str = ""

    @property
    def who(self) -> str:
        return f"{self.label} ({self.owner})" if self.is_agent and self.owner else (self.label or self.owner)


class NeedsPerson(Forbidden):
    """An agent tried an act the org reserves for a person (`needs_person`)."""
    code = "needs_person"


def _prd_refused(a: str, b: str) -> Forbidden:
    # One message for a PRD in another org, one the caller cannot read, and one that does not
    # exist — anything else tells a caller which ids are real in a tenant they are not in.
    return Forbidden(
        f"cannot link {a!r} and {b!r}: a PRD link joins two PRDs inside one organization, and "
        "one of them is not that — it does not exist, you cannot read it, or it is in another "
        "organization (cross-org links are always refused)",
        hint="use a PRD id from another project of this organization",
    )


def _get_prd(db: Session, ref: str) -> Prd | None:
    return db.get(Prd, keys.resolve_prd(db, ref) or ref)


def get_prd_link(db: Session, link_id: str) -> PrdLink | None:
    return db.get(PrdLink, link_id)


def _pair_link(db: Session, x: str, y: str) -> PrdLink | None:
    return db.scalar(select(PrdLink).where(
        or_((PrdLink.prd_a_id == x) & (PrdLink.prd_b_id == y),
            (PrdLink.prd_a_id == y) & (PrdLink.prd_b_id == x))))


def request_prd_link(db: Session, caller: Caller, a: str, b: str, *, reason: str) -> PrdLink:
    """Request a link from PRD `a` (the caller's, writable) to PRD `b` (another project, same
    org, readable or not — a missing, unreadable or cross-org `b` all get the one refusal).

    One link per pair: a pending or accepted pair returns the existing link. A declined or
    unlinked one is reopened as pending under the same id, so its thread stays the pair's one
    thread."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a link request needs a one-line reason")
    pa, pb = _get_prd(db, a), _get_prd(db, b)
    if pa is None or pb is None or pa.project_id not in caller.readable:
        raise _prd_refused(a, b)
    if _org_of(db, pa.project_id) != _org_of(db, pb.project_id):
        raise _prd_refused(a, b)
    # `b` is named by id, not read: the link shows its id/title/status to the pair only, and
    # the project must exist inside this org — which the org check above already settled.
    if pa.project_id == pb.project_id:
        raise ValueError("a PRD link joins PRDs in different projects")
    if pa.project_id not in caller.writable:
        raise Forbidden(
            f"PRD {a!r} is outside this key's write scope; a link is requested from a PRD you can write",
            hint="request the link from a PRD in a project you can write")
    existing = _pair_link(db, pa.id, pb.id)
    if existing is not None and existing.status in ("pending", "accepted"):
        return existing
    if existing is not None:
        existing.prd_a_id, existing.prd_b_id = pa.id, pb.id
        existing.project_a_id, existing.project_b_id = pa.project_id, pb.project_id
        existing.status, existing.reason = "pending", reason
        existing.decision_reason, existing.decided_by, existing.decided_at = "", "", None
        existing.requested_by = caller.who
        link = existing
    else:
        link = PrdLink(
            id="plk_" + uuid.uuid4().hex[:12], prd_a_id=pa.id, prd_b_id=pb.id,
            project_a_id=pa.project_id, project_b_id=pb.project_id, status="pending",
            reason=reason, requested_by=caller.who)
        db.add(link)
    db.commit()
    db.refresh(link)
    return link


def _pending_for_decision(db: Session, caller: Caller, link_id: str) -> PrdLink:
    link = db.get(PrdLink, link_id)
    # B's side only: anyone else (including A's side) gets the same answer as a missing link.
    if link is None or link.project_b_id not in caller.readable:
        raise Forbidden(f"no PRD link {link_id!r} is waiting on you",
                        hint="list the links on your PRD to find the pending request")
    if link.status != "pending":
        raise ValueError(f"link {link_id} is {link.status}, not pending")
    if link.project_b_id not in caller.writable:
        raise Forbidden("deciding a link needs write access to the receiving project")
    return link


def accept_prd_link(db: Session, caller: Caller, link_id: str) -> PrdLink:
    """Accept on B's side. By default only a person may; the org setting
    `prd_link_accept_agents` lets a receiving agent too. An agent refused here gets
    `needs_person` and the link stays pending for a person."""
    link = _pending_for_decision(db, caller, link_id)
    if caller.is_agent:
        allowed = db.scalar(select(Organization.prd_link_accept_agents).where(
            Organization.id == _org_of(db, link.project_b_id)))
        if not allowed:
            raise NeedsPerson(
                f"needs_person: link {link_id} is waiting for a person with write access to "
                "the receiving project; this organization does not let an agent accept",
                hint="needs_person")
    link.status, link.decided_by, link.decided_at = "accepted", caller.who, utcnow()
    db.commit()
    db.refresh(link)
    return link


def decline_prd_link(db: Session, caller: Caller, link_id: str, *, reason: str) -> PrdLink:
    """Decline on B's side, with a reason. Same authority as accept."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a decline needs a reason")
    link = _pending_for_decision(db, caller, link_id)
    if caller.is_agent and not db.scalar(select(Organization.prd_link_accept_agents).where(
            Organization.id == _org_of(db, link.project_b_id))):
        raise NeedsPerson(
            f"needs_person: link {link_id} is decided by a person in this organization",
            hint="needs_person")
    link.status, link.decision_reason = "declined", reason
    link.decided_by, link.decided_at = caller.who, utcnow()
    db.commit()
    db.refresh(link)
    return link


def unlink_prd_link(db: Session, caller: Caller, link_id: str) -> PrdLink:
    """A person with write access on either side unlinks. The thread becomes read-only and is
    kept. Idempotent on an already-unlinked link."""
    link = db.get(PrdLink, link_id)
    if link is None or not ({link.project_a_id, link.project_b_id} & set(caller.readable)):
        raise Forbidden(f"no PRD link {link_id!r} that you can see")
    if caller.is_agent:
        raise NeedsPerson("needs_person: unlinking is a person's act", hint="needs_person")
    if not ({link.project_a_id, link.project_b_id} & set(caller.writable)):
        raise Forbidden("unlinking needs write access to either side's project")
    if link.status != "unlinked":
        link.status, link.decided_by, link.decided_at = "unlinked", caller.who, utcnow()
        db.commit()
        db.refresh(link)
    return link


def list_prd_links(db: Session, caller: Caller, prd: str | None = None) -> list[PrdLink]:
    """Links with at least one side in a project the caller reads, optionally for one PRD."""
    stmt = select(PrdLink).where(or_(PrdLink.project_a_id.in_(caller.readable),
                                     PrdLink.project_b_id.in_(caller.readable)))
    if prd:
        pid = keys.resolve_prd(db, prd) or prd
        stmt = stmt.where(or_(PrdLink.prd_a_id == pid, PrdLink.prd_b_id == pid))
    return list(db.scalars(stmt.order_by(PrdLink.created_at)).all())


def render_prd_link(db: Session, caller: Caller, link: PrdLink) -> dict:
    """The link as the caller sees it: each side's id, title and status — never a body."""
    def side(prd_id: str) -> dict:
        p = db.get(Prd, prd_id)
        return {"id": p.key, "title": p.title, "status": p.status} if p else {"id": prd_id}
    return {"id": link.id, "status": link.status, "reason": link.reason,
            "decision_reason": link.decision_reason, "requested_by": link.requested_by,
            "decided_by": link.decided_by, "from": side(link.prd_a_id), "to": side(link.prd_b_id),
            "created_at": link.created_at.isoformat() if link.created_at else None,
            "decided_at": link.decided_at.isoformat() if link.decided_at else None}
