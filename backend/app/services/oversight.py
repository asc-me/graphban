"""Human oversight of PRD links and their threads (GRPH-P104 S5).

One org-level read of every pending link request and every open pair thread, the stale flag
and alert for what has sat undecided too long, and the retention rule: when a PRD closes or the
pair is unlinked the thread is read-only and an undecided `suggested_item` is withdrawn as
`declined` with code `prd_closed` or `unlinked`. Nothing is deleted. People act through the
existing services (accept/decline, post an answer, file/decline a suggestion), which audit
themselves; this module only reads, sweeps and withdraws.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Alert, Organization, Prd, PrdLink, PrdLinkMessage, Project, User, utcnow
from app.security import authz
from app.services import events as events_svc
from app.services import threads as threads_svc
from app.services.links import Caller
from app.services.question_recipients import link_recipients

KIND = "link_stale"
DEFAULT_STALE_HOURS = 72


def caller_for(db: Session, user: User) -> Caller:
    """A signed-in person as the link services see them."""
    return Caller(readable=authz.readable_project_ids(db, user.id),
                  writable=authz.writable_project_ids(db, user.id),
                  is_agent=False, label=user.name, owner=user.name)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _age_seconds(dt: datetime | None, now: datetime) -> int:
    dt = _aware(dt)
    return max(0, int((now - dt).total_seconds())) if dt else 0


def _receiver_project(link: PrdLink, msg: PrdLinkMessage) -> str:
    """The side that has to act on a message: the one that did not send it."""
    return link.project_b_id if msg.project_id == link.project_a_id else link.project_a_id


def _stale_hours(db: Session, project_id: str) -> int:
    org_id = db.scalar(select(Project.org_id).where(Project.id == project_id))
    hours = db.scalar(select(Organization.prd_link_stale_hours).where(Organization.id == org_id)) \
        if org_id else None
    return hours if hours and hours > 0 else DEFAULT_STALE_HOURS


# ---- retention -------------------------------------------------------------------------------

def withdraw_undecided(db: Session, link: PrdLink) -> int:
    """Withdraw every undecided suggestion on a link whose thread is now read-only because the
    pair was unlinked or either PRD closed. Items already filed are untouched. Idempotent."""
    if link.status == "unlinked":
        code = "unlinked"
    elif link.status == "accepted" and threads_svc.read_only_reason(db, link):
        code = "prd_closed"
    else:
        return 0
    why = threads_svc.read_only_reason(db, link) or f"the link is {link.status}"
    rows = db.scalars(select(PrdLinkMessage).where(
        PrdLinkMessage.link_id == link.id, PrdLinkMessage.kind == "suggested_item",
        PrdLinkMessage.outcome == "")).all()
    done = []
    for m in rows:
        m.outcome, m.outcome_code, m.outcome_message = "declined", code, why
        m.decided_by, m.decided_at = "system", utcnow()
        done.append(m.id)
    if done:
        db.commit()
        for mid in done:
            events_svc.record_prd_link(db, None, action="prd_link_suggestion_withdrawn", link=link,
                                       meta={"message": mid, "outcome": "declined", "code": code})
    return len(done)


def on_prd_closed(db: Session, prd_id: str) -> int:
    """`close_prd` calls this: every accepted link on the PRD becomes read-only now, so its
    undecided suggestions are withdrawn with `prd_closed` at once rather than at the next
    sweep, and the stale alerts on those links clear."""
    links = db.scalars(select(PrdLink).where(
        PrdLink.status == "accepted", or_(PrdLink.prd_a_id == prd_id, PrdLink.prd_b_id == prd_id)
    )).all()
    n = sum(withdraw_undecided(db, link) for link in links)
    _clear_for_links(db, [link.id for link in links])
    return n


def _clear_for_links(db: Session, link_ids: list[str]) -> int:
    """Clear every live stale alert on these links: the thing it named was decided or withdrawn."""
    if not link_ids:
        return 0
    ids = set(link_ids)
    now, n = utcnow(), 0
    for row in db.scalars(select(Alert).where(Alert.kind == KIND, Alert.cleared_at.is_(None))):
        if (row.item_id or "").split(":")[0] in ids:
            row.cleared_at, n = now, n + 1
    db.commit()
    return n


# ---- what is waiting on a person ---------------------------------------------------------------

def _waiting(db: Session, link: PrdLink, now: datetime) -> list[dict]:
    """The things on a link that need a decision: the pending request itself, each unanswered
    question and each undecided suggestion. `key` is the stable alert key."""
    out = []
    if link.status == "pending":
        out.append({"key": link.id, "type": "link_request", "project": link.project_b_id,
                    "prd": link.prd_b_id, "since": link.created_at,
                    "title": f"link request: {link.reason}"})
        return out
    if link.status != "accepted" or threads_svc.read_only_reason(db, link):
        return out
    msgs = db.scalars(select(PrdLinkMessage).where(PrdLinkMessage.link_id == link.id)
                      .order_by(PrdLinkMessage.id)).all()
    answered = {m.reply_to_id for m in msgs if m.reply_to_id}
    for m in msgs:
        if m.kind == "question" and m.id not in answered:
            project = _receiver_project(link, m)
        elif m.kind == "suggested_item" and not m.outcome:
            project = _receiver_project(link, m)
        else:
            continue
        title = m.body if m.kind == "question" else json.loads(m.body).get("title", "")
        out.append({"key": f"{link.id}:{m.id}", "type": m.kind, "project": project,
                    "prd": link.prd_b_id if project == link.project_b_id else link.prd_a_id,
                    "since": m.created_at, "title": f"{m.kind}: {title}"[:200], "message": m.id})
    return out


def _is_stale(db: Session, w: dict, now: datetime) -> bool:
    return _age_seconds(w["since"], now) >= _stale_hours(db, w["project"]) * 3600


def sweep(db: Session, org_id: str | None = None) -> dict:
    """Settle retention, then raise an alert for every stale undecided thing and clear the
    alert of anything decided. One live alert per (thing, recipient). Commits."""
    now = utcnow()
    stmt = select(PrdLink)
    if org_id:
        ids = select(Project.id).where(Project.org_id == org_id)
        stmt = stmt.where(or_(PrdLink.project_a_id.in_(ids), PrdLink.project_b_id.in_(ids)))
    links = db.scalars(stmt).all()
    withdrawn = sum(withdraw_undecided(db, link) for link in links)
    live: dict[str, dict] = {}
    for link in links:
        for w in _waiting(db, link, now):
            if _is_stale(db, w, now):
                live[w["key"]] = {**w, "link": link}
    raised = 0
    for key, w in live.items():
        prd = db.get(Prd, w["prd"])
        recipients = link_recipients(db, prd, w["project"])["users"]
        have = set(db.scalars(select(Alert.user_id).where(
            Alert.kind == KIND, Alert.item_id == key, Alert.cleared_at.is_(None))))
        for uid in recipients:
            if uid in have:
                continue
            db.add(Alert(id="al_" + uuid.uuid4().hex[:12], user_id=uid, project_id=w["project"],
                         kind=KIND, item_id=key, title=w["title"],
                         body=f"Undecided for {_age_seconds(w['since'], now) // 3600}h on PRD link "
                              f"{w['link'].id}."))
            raised += 1
    cleared = 0
    for row in db.scalars(select(Alert).where(Alert.kind == KIND, Alert.cleared_at.is_(None))):
        if row.item_id not in live and (not org_id or row.item_id.split(":")[0] in {l.id for l in links}):
            row.cleared_at = now
            cleared += 1
    db.commit()
    return {"withdrawn": withdrawn, "raised": raised, "cleared": cleared}


# ---- the view ---------------------------------------------------------------------------------

def overview(db: Session, user: User, org_id: str) -> dict:
    """Every pending request and open pair thread in the org that the user can see, with age,
    latest kind, each suggestion's outcome and a stale flag. Sweeps first, so what it shows and
    what it alerted agree."""
    role = authz.require_org_member(db, user.id, org_id)
    sweep(db, org_id)
    from app.services import links as links_svc
    caller = caller_for(db, user)
    org_projects = set(db.scalars(select(Project.id).where(Project.org_id == org_id)))
    if authz.org_admin_rank(role):
        # An org owner or admin oversees EVERY link in the org, including ones between projects
        # they hold no seat on: they are the stale alert's last recipient, so a view that hid the
        # thing they were alerted about would leave them nothing to act on. Seeing is widened;
        # deciding still goes through `caller_for`, so it needs ordinary write access.
        caller = Caller(readable=sorted(org_projects | set(caller.readable)),
                        writable=caller.writable, is_agent=False, label=caller.label,
                        owner=caller.owner)
    now = utcnow()
    rows = []
    for link in links_svc.list_prd_links(db, caller):
        if link.project_a_id not in org_projects or link.status not in ("pending", "accepted"):
            continue
        reason = threads_svc.read_only_reason(db, link) if link.status == "accepted" else None
        if reason:
            continue  # read-only: the thread is kept, but it is no longer open
        msgs = db.scalars(select(PrdLinkMessage).where(PrdLinkMessage.link_id == link.id)
                          .order_by(PrdLinkMessage.id)).all()
        waiting = {w["key"]: w for w in _waiting(db, link, now)}
        suggestions = [{
            "message": m.id,
            "title": json.loads(m.body).get("title", ""),
            "outcome": m.outcome or "undecided", "code": m.outcome_code,
            "item": m.outcome_item_id or None,
            "stale": f"{link.id}:{m.id}" in waiting and _is_stale(db, waiting[f"{link.id}:{m.id}"], now),
        } for m in msgs if m.kind == "suggested_item"]
        questions = [{"message": m.id, "body": m.body,
                      "stale": f"{link.id}:{m.id}" in waiting
                      and _is_stale(db, waiting[f"{link.id}:{m.id}"], now)}
                     for m in msgs if m.kind == "question" and f"{link.id}:{m.id}" in waiting]
        latest = msgs[-1] if msgs else None
        last_at = latest.created_at if latest else link.created_at
        rows.append({
            "link": messages_svc_render_link(db, caller, link),
            "projects": {"from": link.project_a_id, "to": link.project_b_id},
            "status": link.status,
            "latest_kind": latest.kind if latest else None,
            "age_seconds": _age_seconds(last_at, now),
            "suggestions": suggestions, "questions": questions,
            "stale": any(_is_stale(db, w, now) for w in waiting.values()),
            "can_decide": link.project_b_id in caller.writable,
        })
    rows.sort(key=lambda r: (not r["stale"], -r["age_seconds"]))
    return {"stale_after_hours": _org_hours(db, org_id), "threads": rows}


def messages_svc_render_link(db, caller, link) -> dict:
    from app.services import links as links_svc
    return links_svc.render_prd_link(db, caller, link)


def _org_hours(db: Session, org_id: str) -> int:
    hours = db.scalar(select(Organization.prd_link_stale_hours).where(Organization.id == org_id))
    return hours if hours and hours > 0 else DEFAULT_STALE_HOURS


def set_stale_hours(db: Session, user: User, org_id: str, hours: int) -> int:
    """The org sets the age past which an undecided thing is stale. Owners and admins only."""
    role = authz.require_org_member(db, user.id, org_id)
    if role not in ("owner", "admin"):
        from fastapi import HTTPException
        raise HTTPException(403, "only an owner or admin sets the stale age")
    if hours < 1:
        raise ValueError("the stale age is at least one hour")
    db.get(Organization, org_id).prd_link_stale_hours = hours
    db.commit()
    return hours
