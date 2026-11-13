"""Alerts: the inbox rows behind the bell (GRPH-1527, P75 D4).

Writes never commit: `waits.hand_to_person` writes the rows and stamps `needs_person_at` in
ONE transaction, so a failed write leaves the wait unhanded and the next sweep retries it.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, Item, Link, Project, utcnow
from app.security.authz import readable_project_ids

KIND = "response_needed"


def write_for_wait(db: Session, wait: Item, user_ids: list[str], *, body: str = "") -> list[Alert]:
    """One alert per recipient for this wait. Adds to the session; the caller commits."""
    rows = []
    for uid in dict.fromkeys(user_ids):
        row = Alert(id="al_" + uuid.uuid4().hex[:12], user_id=uid, project_id=wait.project_id,
                    kind=KIND, item_id=wait.id,
                    title=(wait.decision or {}).get("question") or wait.title, body=body)
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def clear_for_item(db: Session, item_id: str) -> int:
    """Set `cleared_at` on every live alert for this wait. Caller commits."""
    now = utcnow()
    n = 0
    for row in db.scalars(select(Alert).where(Alert.item_id == item_id, Alert.cleared_at.is_(None))):
        row.cleared_at = now
        n += 1
    return n


def _asker(db: Session, wait: Item) -> Item | None:
    dep_ids = db.scalars(select(Link.a).where(Link.type == "dependency", Link.b == wait.id)).all()
    for dep_id in dep_ids:
        dep = db.get(Item, dep_id)
        if dep is not None:
            return dep
    return None


def alert_dict(db: Session, row: Alert) -> dict:
    wait = db.get(Item, row.item_id)
    ticket = _asker(db, wait) if wait is not None else None
    holder = wait if wait is not None and wait.prd_id else ticket
    prd_key = holder.prd_key if holder is not None and holder.prd_id else None
    # Derived impact (GRPH-1607), never a stored priority: a live drain waiting on this PRD, else
    # a ticket blocked on the wait, else a plain question. An `unknown` drain read claims nothing.
    drain = "unknown"
    if wait is not None:
        from app.services import fleet as fleet_svc

        drain = fleet_svc.drain_status(db, wait.project_id, holder.prd_id if holder is not None else None)
    impact = "drain_waiting" if drain == "live" else "blocks" if ticket is not None else "question"
    project = db.get(Project, row.project_id)
    return {
        "id": row.id, "kind": row.kind, "project": row.project_id,
        "project_tag": project.tag if project is not None else None,
        "title": wait.title if wait is not None else row.title,
        "impact": impact, "drain": drain,
        "question": row.title, "body": row.body,
        "wait": wait.key if wait is not None else row.item_id,
        "ticket": ticket.key if ticket is not None else None,
        "prd": prd_key,
        "asked_by": (ticket.claimed_by or ticket.built_by) if ticket is not None else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "read": row.read_at is not None,
    }


def list_for_user(db: Session, user_id: str) -> dict:
    # D6: access is checked at read time, so an alert for a project the user lost is hidden
    readable = readable_project_ids(db, user_id)
    rows = db.scalars(select(Alert).where(Alert.user_id == user_id, Alert.cleared_at.is_(None),
                                          Alert.project_id.in_(readable))
                      .order_by(Alert.created_at.desc(), Alert.id.desc())).all()
    return {"unread": sum(r.read_at is None for r in rows),
            "alerts": [alert_dict(db, r) for r in rows]}


def mark_read(db: Session, user_id: str, alert_id: str) -> bool:
    row = db.get(Alert, alert_id)
    if row is None or row.user_id != user_id or row.project_id not in readable_project_ids(db, user_id):
        return False
    if row.read_at is None:
        row.read_at = utcnow()
        db.commit()
    return True
