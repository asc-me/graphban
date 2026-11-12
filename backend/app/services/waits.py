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
from app.services import alerts as alerts_svc

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


CHECKING = "checking"
RESPONSE_NEEDED = "response_needed"


def _open_decision_state(wait: Item) -> str | None:
    """The state of ONE wait item: None unless it is an open `wait:decision`."""
    if "wait:decision" not in {str(t).lower() for t in wait.tags or []}:
        return None
    if wait.status == "done":
        return None
    d = wait.decision or {}
    if d.get("answer"):
        return None
    return RESPONSE_NEEDED if d.get("needs_person_at") else CHECKING


def question_state(db: Session, item: Item) -> str | None:
    """None | "checking" | "response_needed" (GRPH-P75 D1). The ONE definition every surface reads.

    An open `wait:decision` reads Checking context until `decision.needs_person_at` is set, then
    Response Needed. A `blocked` ticket that depends on one reads the same. Gate waits (merge,
    deploy, secret, access) and every other item read None, i.e. plain blocked. Answering the
    wait clears it on the wait and on whatever waited on it.
    """
    own = _open_decision_state(item)
    if own is not None:
        return own
    if item.status != "blocked":
        return None
    from sqlalchemy import select

    from app.models import Link

    dep_ids = db.scalars(select(Link.b).where(Link.type == "dependency", Link.a == item.id)).all()
    states = []
    for dep_id in dict.fromkeys(dep_ids):
        dep = db.get(Item, dep_id)
        if dep is not None:
            st = _open_decision_state(dep)
            if st is not None:
                states.append(st)
    if RESPONSE_NEEDED in states:
        return RESPONSE_NEEDED
    return CHECKING if states else None


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
        # `ctx.item`, not `by_id`: a wait filed in another project is still a wait (GRPH-1063),
        # and missing it here let the original move to review past an open human wait.
        dep = ctx.item(dep_id)
        if dep is not None and wait_tags_on(dep):
            out.append(dep.id)
    return out


def release_waiters(db: Session, wait: Item) -> list[str]:
    """After a wait item reaches `done`: blocked dependents with no remaining
    unmet deps return to `next`. Does not rewrite `in_progress`.
    """
    from sqlalchemy import select

    from app.models import Link
    from app.services import prioritization as prio

    # Dependents are found by the LINK, not through the wait's project context: a dependent
    # in another project (GRPH-1063) is never in that context, and was left blocked forever.
    # Each one is then judged in its OWN project's context, where all its deps resolve.
    dependent_ids = db.scalars(
        select(Link.a).where(Link.type == "dependency", Link.b == wait.id)).all()
    contexts: dict[str, prio.Context] = {}
    released: list[str] = []
    for dep_id in dict.fromkeys(dependent_ids):
        dependent = db.get(Item, dep_id)
        if dependent is None or dependent.status != "blocked":
            continue
        ctx = contexts.get(dependent.project_id)
        if ctx is None:
            ctx = contexts[dependent.project_id] = prio.context(db, dependent.project_id)
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


# ---- Structured decisions and blocker actions (GRPH-1137 / PRD-60 D7) -------------------------
#
# A `wait:decision` item may carry `item.decision = {question, options, answer, notify}`.
# Answering it is the human act that unblocks the dependents through `release_waiters`.
# Every action below writes exactly ONE `blocker.*` event whose meta holds a before/after
# snapshot of each item it touched, which is all `undo` needs: it restores `before` only while
# every item still reads `after`. A later change by anyone makes the undo refuse.

ACTIONS = ("answer", "unblock", "release_claim", "escalate", "snooze")
_ACTION_EVENTS = tuple(f"blocker.{a}" for a in ACTIONS)
_SNAP_FIELDS = ("status", "blocker", "claimed_by", "claimed_at", "assignee", "snoozed_until",
                "decision")


class ActionRefused(ValueError):
    """The action does not apply to this item in its current state."""


class OwnWaitRefused(ActionRefused):
    """The agent that filed a decision wait tried to answer it (rule `decision.filer_cannot_answer`)."""


def _iso(v):
    """Aware-UTC ISO, so a snapshot taken before a commit equals one read back after it
    (SQLite hands datetimes back naive)."""
    return _aware(v).isoformat() if hasattr(v, "isoformat") else v


def _snap(item: Item) -> dict:
    return {f: _iso(getattr(item, f)) for f in _SNAP_FIELDS}


def _parse_dt(v):
    from datetime import datetime

    return datetime.fromisoformat(v) if isinstance(v, str) else v


def _restore(item: Item, snap: dict) -> None:
    for f in _SNAP_FIELDS:
        v = snap[f]
        setattr(item, f, _parse_dt(v) if f in ("claimed_at", "snoozed_until") else v)


def _aware(dt):
    from datetime import timezone

    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _lease_live(item: Item) -> bool:
    from datetime import timedelta

    from app.models import utcnow
    from app.services.items import DEFAULT_LEASE_SECONDS

    at = _aware(item.claimed_at)
    return bool(item.claimed_by) and at is not None and (
        utcnow() - at < timedelta(seconds=DEFAULT_LEASE_SECONDS))


def _get(db: Session, item_id: str) -> Item:
    from app.services import keys

    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        raise ActionRefused(f"no item {item_id!r}")
    return item


def decision_dict(item: Item) -> dict | None:
    """The question/options/answer an item read carries. None when it is not a decision."""
    d = item.decision
    if not d:
        return None
    out = {"question": d.get("question", ""), "options": d.get("options") or []}
    if d.get("answer"):
        out["answer"] = d["answer"]
    return out


def set_decision(item: Item, question: str | None, options: list | None) -> None:
    """Attach a question/options to a freshly filed wait. Tags it `wait:decision`."""
    question = (question or "").strip()
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if not question and not opts:
        return
    if opts and not question:
        raise ValueError("`options` need a `question`")
    item.decision = {"question": question, "options": opts}
    tags = list(item.tags or [])
    if "wait:decision" not in {str(t).lower() for t in tags}:
        tags.append("wait:decision")
    item.tags = tags


def _event(db: Session, action: str, items: dict[str, tuple[Item, dict]], *, actor, surface: str,
           meta: dict | None = None, primary: Item) -> int:
    from app.models import Event

    ev = Event(
        actor_type="user", actor_id=getattr(actor, "id", "") or "",
        actor_label=getattr(actor, "handle", "") or getattr(actor, "name", "") or "",
        surface=surface, action=f"blocker.{action}", target_type="item",
        target_id=primary.key, project_id=primary.project_id,
        meta={**(meta or {}), "items": {
            it.id: {"before": before, "after": _snap(it)} for it, before in items.values()}},
    )
    db.add(ev)
    db.flush()
    return ev.id


SOURCE_KINDS = ("session", "prd", "memory", "decision")


def _resolve_source(db: Session, source: dict | None) -> dict:
    """Validate an agent answer's source. `ref` must resolve for prd, memory and decision."""
    from app.models import MemoryShard, Prd
    from app.services import keys

    if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
        raise ValueError(f"an agent answer needs a `source` with kind one of {', '.join(SOURCE_KINDS)}")
    kind = source["kind"]
    ref, quote = str(source.get("ref") or "").strip(), str(source.get("quote") or "").strip()
    if kind in ("prd", "memory", "decision") and not ref:
        raise ValueError(f"a {kind} source needs a `ref`")
    if kind == "prd":
        pid = keys.resolve_prd(db, ref) or ref
        if db.get(Prd, pid) is None:
            raise ValueError(f"source ref {ref!r} does not resolve to a PRD")
    elif kind == "memory":
        if db.get(MemoryShard, ref) is None:
            raise ValueError(f"source ref {ref!r} does not resolve to a memory shard")
    elif kind == "decision":
        other = db.get(Item, keys.resolve_item(db, ref) or ref)
        if other is None or not (other.decision or {}).get("answer"):
            raise ValueError(f"source ref {ref!r} does not resolve to an answered decision")
    return {"kind": kind, "ref": ref, "quote": quote}


def drain_note(db: Session, wait: Item, status: str | None = None) -> dict:
    """The drain-status sentence for an answered wait (GRPH-1529).

    The PRD is the asking ticket's (a dependent), falling back to the wait's own. `status`
    is injectable so a failed read can be shown without a broken database.
    """
    from sqlalchemy import select

    from app.models import Link
    from app.services import fleet as fleet_svc

    holder = wait
    if not wait.prd_id:
        for dep_id in db.scalars(select(Link.a).where(Link.type == "dependency", Link.b == wait.id)):
            dep = db.get(Item, dep_id)
            if dep is not None and dep.prd_id:
                holder = dep
                break
    prd_key = holder.prd_key if holder.prd_id else None
    status = status or fleet_svc.drain_status(db, wait.project_id, holder.prd_id)
    label = prd_key or "this project"
    note: dict = {"status": status, "prd": prd_key}
    if status == "live":
        note["message"] = f"A drain is running for {label}. It will pick this ticket up."
    elif status == "not_live":
        note["message"] = f"No drain is running for {label}."
        note["command"] = f"gban fleet until --prd {prd_key}" if prd_key else "gban fleet until"
    else:
        note["message"] = "Could not check for a running drain."
    return note


def answered_by_line(answer: dict) -> str:
    """"answered by <person>" or "answered from <source> by <agent>" for the Triage row."""
    by = answer.get("by") or {}
    src = answer.get("source")
    if by.get("agent") and src:
        return f"answered from {src.get('kind')} by {by['agent']}"
    return f"answered by {by.get('user') or 'a person'}"


def hand_to_person(db: Session, wait: Item) -> dict:
    """Hand an open decision wait to a person: one transaction (GRPH-1527, P75 D4).

    Re-reads the wait under a row lock. Answered, or already handed: a no-op. Otherwise writes
    one alert per recipient and stamps `needs_person_at` in the SAME commit, so alerts that
    fail to write leave the wait unhanded for the next sweep. With no recipient no alert is
    written and `decision.nobody_alerted` records it for the Triage row.
    """
    from sqlalchemy import select

    from app.models import Link, utcnow
    from app.services.question_recipients import question_recipients

    try:
        locked = db.execute(select(Item).where(Item.id == wait.id).with_for_update()
                            .execution_options(populate_existing=True)).scalar_one()
        d = locked.decision or {}
        if locked.status == "done" or d.get("answer") or d.get("needs_person_at"):
            db.rollback()
            return {"handed": False, "alerts": 0}
        holder = locked
        if not locked.prd_id:
            for dep_id in db.scalars(select(Link.a).where(Link.type == "dependency",
                                                          Link.b == locked.id)):
                dep = db.get(Item, dep_id)
                if dep is not None and dep.prd_id:
                    holder = dep
                    break
        recipients = question_recipients(db, holder)
        users = list(recipients["users"])
        rows = alerts_svc.write_for_wait(db, locked, users,
                                         body=f"{locked.key}: {recipients['reason']}") if users else []
        new = {**d, "needs_person_at": _iso(utcnow())}
        if not users:
            new["nobody_alerted"] = True
        locked.decision = new
        db.commit()
        db.refresh(wait)
        return {"handed": True, "alerts": len(rows)}
    except Exception:
        db.rollback()
        raise


def sweep_questions(db: Session, *, grace_seconds: int | None = None, now=None) -> list[str]:
    """Hand every open decision wait older than the grace period to a person. Returns the keys
    handed. One wait failing is rolled back and left for the next pass."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.config import settings
    from app.models import utcnow

    grace = settings.question_grace_seconds if grace_seconds is None else grace_seconds
    cutoff = _aware(now or utcnow()) - timedelta(seconds=grace)
    rows = db.scalars(select(Item).where(Item.decision.is_not(None), Item.status != "done")).all()
    due = [w.id for w in rows
           if "wait:decision" in {str(t).lower() for t in w.tags or []}
           and not (w.decision or {}).get("answer") and not (w.decision or {}).get("needs_person_at")
           and _aware(w.created_at) <= cutoff]
    handed = []
    for wid in due:
        try:
            wait = db.get(Item, wid)
            if wait is not None and hand_to_person(db, wait)["handed"]:
                handed.append(wait.key)
        except Exception:  # noqa: BLE001 - the next sweep retries it
            db.rollback()
    return handed


def answer_wait(db: Session, wait_id: str, *, choice: str | None = None, text: str | None = None,
                actor=None, surface: str = "rest", agent: str | None = None,
                source: dict | None = None, needs_person: bool = False) -> dict:
    """Record the answer on a decision wait, mark it done, and release what waited on it.

    An agent (`agent` set) must name a `source`; a person's answer carries none. `needs_person`
    records no answer: it only stamps `decision.needs_person_at` (S4 hands it to a person).
    """
    from sqlalchemy import select

    from app.models import Link, utcnow

    wait = _get(db, wait_id)
    if not wait.decision or "wait:decision" not in {str(t).lower() for t in wait.tags or []}:
        raise ActionRefused(f"{wait.key} is not a decision wait")
    if wait.status == "done" or wait.decision.get("answer"):
        raise ActionRefused(f"{wait.key} is already answered")
    if agent:
        dependents = db.scalars(select(Item).join(Link, Link.a == Item.id).where(
            Link.type == "dependency", Link.b == wait.id)).all()
        askers = {who for d in dependents for who in (d.claimed_by, d.built_by) if who}
        if agent in askers:
            raise OwnWaitRefused(f"{agent} filed the question on {wait.key} and cannot answer it; "
                                "another agent or a person must")
    if needs_person:
        handed = hand_to_person(db, wait)
        return {"wait": wait.key, "needs_person": True, "released": [], "handed": handed["handed"]}
    src = _resolve_source(db, source) if agent else None
    choice, text = (choice or "").strip(), (text or "").strip()
    if not choice and not text:
        raise ValueError("an answer needs a chosen `option` or free `text`")
    options = wait.decision.get("options") or []
    if choice and options and choice not in options:
        raise ValueError(f"{choice!r} is not one of the options: {', '.join(options)}")

    dependent_ids = list(dict.fromkeys(db.scalars(
        select(Link.a).where(Link.type == "dependency", Link.b == wait.id)).all()))
    touched: dict[str, tuple[Item, dict]] = {wait.id: (wait, _snap(wait))}
    notify: list[str] = []
    for dep_id in dependent_ids:
        dep = db.get(Item, dep_id)
        if dep is None:
            continue
        touched[dep.id] = (dep, _snap(dep))
        # Told BEFORE the release clears the lease: the holder is the agent that asked.
        if dep.claimed_by and _lease_live(dep) and dep.claimed_by not in notify:
            notify.append(dep.claimed_by)

    user = getattr(actor, "handle", "") or getattr(actor, "name", "") or ""
    if agent:
        answer = {"choice": choice, "text": text, "by": {"agent": agent, "user": user},
                  "source": src, "at": int(utcnow().timestamp())}
    else:
        answer = {"choice": choice, "text": text, "by": {"user": user},
                  "at": int(utcnow().timestamp())}
    wait.decision = {**wait.decision, "answer": answer, "notify": notify, "notified": []}
    # Through `update_item`, the one gated writer of `done`: it clears a typed wait without an
    # attestation and releases its dependents (`release_waiters`).
    from app.services import items as items_svc

    items_svc.update_item(db, wait.id, status="done", blocker="")
    db.refresh(wait)
    for dep, _ in touched.values():
        db.refresh(dep)
    released = [d.key for d, b in touched.values()
                if d is not wait and b["status"] == "blocked" and d.status != "blocked"]
    eid = _event(db, "answer", touched, actor=actor, surface=surface, primary=wait,
                 meta={"released": released})
    alerts_svc.clear_for_item(db, wait.id)
    db.commit()
    db.refresh(wait)
    return {"wait": wait.key, "answer": answer, "released": released, "event_id": eid,
            "answered_by": answered_by_line(answer),
            "quote": (answer.get("source") or {}).get("quote"),
            "drain": drain_note(db, wait)}


def pending_answers(db: Session, agent_id: str) -> list[Item]:
    """Answered decision waits that still owe this agent a directive."""
    from sqlalchemy import select

    rows = db.scalars(select(Item).where(Item.decision.is_not(None), Item.status == "done")
                      .order_by(Item.done_at.desc()).limit(100)).all()
    return [w for w in rows
            if agent_id in (w.decision.get("notify") or [])
            and agent_id not in (w.decision.get("notified") or [])]


def answer_directive(db: Session, agent_id: str) -> dict | None:
    """The next answer the live holder has not heard, marked delivered. None if nothing waits."""
    waits = pending_answers(db, agent_id)
    if not waits:
        return None
    wait = waits[0]
    wait.decision = {**wait.decision, "notified": [*(wait.decision.get("notified") or []), agent_id]}
    db.commit()
    return {
        "type": "decision_answered",
        "wait": wait.key,
        "question": wait.decision.get("question", ""),
        "answer": wait.decision["answer"],
        "next": "the blocked item is back in `next`: claim it again and continue with this answer",
    }


def unblock(db: Session, item_id: str, *, actor=None, surface: str = "rest") -> dict:
    """Clear the blocker. `next`, or `in_progress` when the lease is still live."""
    item = _get(db, item_id)
    if item.status != "blocked":
        raise ActionRefused(f"{item.key} is {item.status}, not blocked")
    before = _snap(item)
    item.blocker = ""
    item.status = "in_progress" if _lease_live(item) else "next"
    eid = _event(db, "unblock", {item.id: (item, before)}, actor=actor, surface=surface,
                 primary=item)
    db.commit()
    return {"item": item.key, "status": item.status, "event_id": eid}


def release_claim(db: Session, item_id: str, *, actor=None, surface: str = "rest") -> dict:
    """Drop the lease and return the item to `next` with no holder."""
    from app.services import fleet as fleet_svc

    item = _get(db, item_id)
    if not item.claimed_by:
        raise ActionRefused(f"{item.key} has no holder")
    if item.status in ("done", "review"):
        raise ActionRefused(f"{item.key} is {item.status}; there is no build claim to release")
    before = _snap(item)
    fleet_svc.release_reservations(db, item_id=item.id)
    item.claimed_by = None
    item.claimed_at = None
    item.assignee = ""
    item.status = "next"
    eid = _event(db, "release_claim", {item.id: (item, before)}, actor=actor, surface=surface,
                 primary=item)
    db.commit()
    return {"item": item.key, "status": item.status, "event_id": eid}


def snooze(db: Session, item_id: str, *, until, actor=None, surface: str = "rest") -> dict:
    """Hide the blocker from triage until `until` (a datetime in the future)."""
    from app.models import utcnow

    item = _get(db, item_id)
    until = _aware(until)
    if until is None or until <= utcnow():
        raise ValueError("`until` must be in the future")
    before = _snap(item)
    item.snoozed_until = until
    eid = _event(db, "snooze", {item.id: (item, before)}, actor=actor, surface=surface,
                 primary=item, meta={"until": until.isoformat()})
    db.commit()
    return {"item": item.key, "snoozed_until": until.isoformat(), "event_id": eid}


def escalate(db: Session, item_id: str, *, actor=None, surface: str = "rest") -> dict:
    """Record an escalation and email the project owner(s) when email is configured.

    Nothing changes on the item, so there is no state to restore: undoing it retracts the
    record but cannot unsend the email.
    """
    from sqlalchemy import select

    from app.config import settings
    from app.models import Membership, User
    from app.services.email import send_email

    item = _get(db, item_id)
    to: list[str] = []
    if settings.smtp_host:
        owners = db.scalars(select(Membership.user_id).where(
            Membership.project_id == item.project_id, Membership.role == "owner")).all()
        to = [u.email for u in (db.get(User, o) for o in owners) if u is not None and u.email]
    eid = _event(db, "escalate", {item.id: (item, _snap(item))}, actor=actor, surface=surface,
                 primary=item, meta={"emailed": len(to)})
    db.commit()
    for addr in to:
        send_email(addr, f"[Graphban] {item.key} needs attention",
                   f"{item.key} {item.title} was escalated from triage.\n"
                   f"Blocker: {item.blocker or '(none)'}")
    return {"item": item.key, "emailed": len(to), "event_id": eid}


def undo(db: Session, item_id: str, *, actor=None, surface: str = "rest") -> dict:
    """Undo the newest blocker action on this item, if nothing has changed since."""
    from sqlalchemy import select

    from app.models import Event

    item = _get(db, item_id)
    last = db.scalar(select(Event).where(
        Event.target_type == "item", Event.target_id == item.key,
        Event.action.like("blocker.%")).order_by(Event.id.desc()).limit(1))
    if last is None or last.action not in _ACTION_EVENTS:
        raise ActionRefused(f"nothing to undo on {item.key}")
    snaps = (last.meta or {}).get("items") or {}
    rows = {i: db.get(Item, i) for i in snaps}
    for i, row in rows.items():
        if row is None or _snap(row) != snaps[i]["after"]:
            raise ActionRefused(
                f"{item.key} changed after `{last.action}`; undo is only for the latest change")
    for i, row in rows.items():
        _restore(row, snaps[i]["before"])
    ev = Event(
        actor_type="user", actor_id=getattr(actor, "id", "") or "",
        actor_label=getattr(actor, "handle", "") or getattr(actor, "name", "") or "",
        surface=surface, action="blocker.undo", target_type="item", target_id=item.key,
        project_id=item.project_id, meta={"undoes": last.id, "action": last.action})
    db.add(ev)
    db.commit()
    return {"item": item.key, "undone": last.action, "event_id": ev.id}


def triage_blockers(db: Session, project_id: str | None = None) -> list[dict]:
    """Blocked items for triage, snoozed ones hidden. Decision waits carry question/options."""
    from app.models import utcnow
    from app.services import items as items_svc

    now = utcnow()
    out = []
    for it in items_svc.list_items(db, project_id=project_id, status="blocked"):
        until = _aware(it.snoozed_until)
        if until is not None and until > now:
            continue
        row = {"id": it.key, "title": it.title, "blocker": it.blocker,
               "tags": it.tags, "claimed_by": it.claimed_by}
        d = decision_dict(it)
        if d:
            row["decision"] = d
        out.append(row)
    return out
