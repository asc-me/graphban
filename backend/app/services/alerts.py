"""Alerts: the inbox rows behind the bell (GRPH-1527, P75 D4).

Writes never commit: `waits.hand_to_person` writes the rows and stamps `needs_person_at` in
ONE transaction, so a failed write leaves the wait unhanded and the next sweep retries it.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tagging
from app.models import Alert, Item, Link, Prd, Project, utcnow
from app.security.authz import readable_project_ids

KIND = "response_needed"
#: Drain notice outcomes (GRPH-1913, P99) -> the alert kind each writes.
DRAIN_KINDS = {"drained": "drain_drained", "stalled": "drain_stalled"}
DRAIN_OUTCOMES = (*DRAIN_KINDS, "working")
#: Web Push for a drain fires on a change of outcome kind, else at most this often per PRD.
DRAIN_PUSH_GAP = timedelta(minutes=30)


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


def _aware(dt):
    from datetime import timezone

    return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _drain_text(prd_key: str, outcome: str, open_items: list[dict]) -> tuple[str, str]:
    if outcome == "drained":
        return f"{prd_key} drained", f"The drain for {prd_key} finished: nothing is left to build."
    lines = [f"{o['item']}: {o['reason']}" if o.get("reason") else str(o["item"]) for o in open_items]
    return f"{prd_key} stalled", "\n".join(lines)


def post_drain_notice(db: Session, project_id: str, prd: Prd, outcome: str,
                      open_items: list[dict] | None = None) -> dict:
    """Turn a drain notice into the PRD's live alert (GRPH-1913, P99 D6/D7). Commits.

    One live alert per (PRD, recipient). `working` clears it. The same outcome with the same
    text changes nothing; the same outcome with new text updates the row in place; a changed
    outcome replaces the row. Returns `{"alerts": [ids], "push": [ids to push]}` — the caller
    pushes AFTER this commit, best-effort.
    """
    from app.services.question_recipients import question_recipients

    now = utcnow()
    live = list(db.scalars(select(Alert).where(
        Alert.prd_id == prd.id, Alert.kind.in_(tuple(DRAIN_KINDS.values())),
        Alert.cleared_at.is_(None)).order_by(Alert.created_at, Alert.id)))
    if outcome == "working":
        for row in live:
            row.cleared_at = now
        db.commit()
        return {"alerts": [], "cleared": len(live), "push": [], "changed": bool(live)}
    kind = DRAIN_KINDS[outcome]
    project = db.get(Project, prd.project_id)
    title, body = _drain_text(tagging.render(project.tag, "prd", prd.number), outcome,
                              open_items or [])
    users = list(dict.fromkeys(question_recipients(
        db, SimpleNamespace(project_id=prd.project_id, prd_id=prd.id))["users"]))
    by_user: dict[str, list[Alert]] = {}
    for row in live:
        by_user.setdefault(row.user_id, []).append(row)
    ids, push = [], []
    changed = False
    for row in live:  # no longer a recipient, or a duplicate beyond the one live row
        if row.user_id not in users or by_user[row.user_id][0] is not row:
            row.cleared_at = now
    for uid in users:
        row = by_user.get(uid, [None])[0]
        if row is not None and row.kind == kind:
            ids.append(row.id)
            if row.title == title and row.body == body:
                continue  # identical to the last notice: nothing changes
            row.title, row.body = title, body
            changed = True
            last = _aware(row.pushed_at)
            if last is None or now - last >= DRAIN_PUSH_GAP:
                row.pushed_at = now
                push.append(row.id)
            continue
        if row is not None:
            row.cleared_at = now  # stalled <-> drained replaces the row
        new = Alert(id="al_" + uuid.uuid4().hex[:12], user_id=uid, project_id=prd.project_id,
                    kind=kind, item_id="", prd_id=prd.id, title=title, body=body, pushed_at=now)
        db.add(new)
        changed = True
        ids.append(new.id)
        push.append(new.id)
    db.commit()
    return {"alerts": ids, "push": push, "changed": changed}


#: Tags that make an item never a build item — fleet's `drain_outcome._build_open` rule.
NOT_BUILD_TAGS = frozenset({"walk", "planner-only"})


def clear_settled_stall(db: Session, prd_id: str | None) -> int:
    """Clear a PRD's live `drain_stalled` alert once it has no open build item (GRPH-1972).

    A stall a person resolves after its drain has stopped has nobody left to post `working`
    or `drained`, so the server clears it when the last open build item leaves the PRD. Not
    turned into `drained`: that stays the drain's notice to send. While another open build
    item remains the alert is left alone, so the drain's next notice keeps owning it.
    Commits when it clears anything. Returns the number of alerts cleared.
    """
    if not prd_id:
        return 0
    live = list(db.scalars(select(Alert).where(
        Alert.prd_id == prd_id, Alert.kind == DRAIN_KINDS["stalled"],
        Alert.cleared_at.is_(None))))
    if not live:
        return 0
    for item in db.scalars(select(Item).where(Item.prd_id == prd_id, Item.status != "done")):
        if not (set(item.tags or ()) & NOT_BUILD_TAGS):
            return 0  # still open build work: the drain's next notice owns the alert
    now = utcnow()
    for row in live:
        row.cleared_at = now
    db.commit()
    return len(live)


def _askers(db: Session, wait_ids: list[str]) -> dict[str, Item]:
    """The asker of each wait: the first item it depends on that still exists.

    One rule, read for a whole list — the links and the items they name in two statements
    rather than two per wait. Ordered by link id so "first" is the same link on both
    engines instead of whichever one the scan happened to return.
    """
    pairs = db.execute(select(Link.b, Link.a)
                       .where(Link.type == "dependency", Link.b.in_(wait_ids))
                       .order_by(Link.id)).all()
    deps = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_({a for _, a in pairs})))}
    askers: dict[str, Item] = {}
    for wait_id, dep_id in pairs:
        if wait_id in askers:
            continue
        dep = deps.get(dep_id)
        if dep is not None:
            askers[wait_id] = dep
    return askers


def _asker(db: Session, wait: Item) -> Item | None:
    return _askers(db, [wait.id]).get(wait.id)


class _Lookup:
    """The rows behind a list of alerts, read once for the whole list (GRPH-1897).

    `alert_dict` used to read the wait, its asker and its project per alert and ask
    `drain_status` per alert, so the bell cost about six statements a row and 6 s on a first
    load. Each of these is one read for the list instead. The drain is memoized per
    (project, PRD) because it answers about that pair, not about a row: the count is flat in
    alerts and in the projects the user can read, and grows only with the distinct pairs the
    listed alerts actually name.
    """

    def __init__(self, db: Session, rows: list[Alert]):
        self.db = db
        wait_ids = [r.item_id for r in rows if r.item_id]
        self.waits = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(wait_ids)))}
        self.askers = _askers(db, wait_ids)
        self.projects = {p.id: p for p in db.scalars(
            select(Project).where(Project.id.in_({r.project_id for r in rows})))}
        self._drain: dict[tuple[str, str | None], str] = {}

    def wait(self, row: Alert) -> Item | None:
        return self.waits.get(row.item_id)

    def drain(self, project_id: str, prd_id: str | None) -> str:
        key = (project_id, prd_id)
        if key not in self._drain:
            from app.services import fleet as fleet_svc

            self._drain[key] = fleet_svc.drain_status(self.db, project_id, prd_id)
        return self._drain[key]


def alert_dict(db: Session, row: Alert, *, lookup: _Lookup | None = None) -> dict:
    """One alert as the bell renders it. `lookup` carries a list's shared reads; without it
    this row's own are made here."""
    seen = lookup if lookup is not None else _Lookup(db, [row])
    if row.prd_id:  # a drain notice: no wait, no asker, no impact to derive
        project = seen.projects.get(row.project_id)
        prd = db.get(Prd, row.prd_id)
        return {
            "id": row.id, "kind": row.kind, "project": row.project_id,
            "project_tag": project.tag if project is not None else None,
            "title": row.title, "impact": "drain",
            "question": row.title, "body": row.body, "wait": None, "ticket": None,
            "prd": tagging.render(project.tag, "prd", prd.number)
            if project is not None and prd is not None else None,
            "asked_by": None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "read": row.read_at is not None,
        }
    wait = seen.wait(row)
    ticket = seen.askers.get(wait.id) if wait is not None else None
    holder = wait if wait is not None and wait.prd_id else ticket
    prd_key = holder.prd_key if holder is not None and holder.prd_id else None
    # Derived impact (GRPH-1607), never a stored priority: a live drain waiting on this PRD, else
    # a ticket blocked on the wait, else a plain question. An `unknown` drain read claims nothing.
    drain = "unknown"
    if wait is not None:
        drain = seen.drain(wait.project_id, holder.prd_id if holder is not None else None)
    impact = "drain_waiting" if drain in ("live", "scheduled") else "blocks" if ticket is not None else "question"
    project = seen.projects.get(row.project_id)
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
    # D6: access is checked at read time, so an alert for a project the user lost is hidden.
    # Checked ONCE for the list, in the query, never again per alert (GRPH-1535, GRPH-1897).
    readable = readable_project_ids(db, user_id)
    rows = db.scalars(select(Alert).where(Alert.user_id == user_id, Alert.cleared_at.is_(None),
                                          Alert.project_id.in_(readable))
                      .order_by(Alert.created_at.desc(), Alert.id.desc())).all()
    lookup = _Lookup(db, rows)
    return {"unread": sum(r.read_at is None for r in rows),
            "alerts": [alert_dict(db, r, lookup=lookup) for r in rows]}


def mark_read(db: Session, user_id: str, alert_id: str) -> bool:
    row = db.get(Alert, alert_id)
    if row is None or row.user_id != user_id or row.project_id not in readable_project_ids(db, user_id):
        return False
    if row.read_at is None:
        row.read_at = utcnow()
        db.commit()
    return True
