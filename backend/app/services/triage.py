"""The unified triage feed (GRPH-1135 / PRD-60 D5): requests, blockers and collisions in one
per-project queue.

Each source is read by the function that already owns its snooze/decision rules —
`requests.is_untriaged`, `waits.triage_blockers`, `collision.triage` — so a row hidden there is
hidden here. This module only shapes and merges them.

An empty `rows` list must never read as "nothing waiting" when a source did not answer, so each
source is read inside its own guard and a failure is named in `failed_sources` while the other
kinds are still returned.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import Item, Request, utcnow
from app.services import collision as collision_svc
from app.services import duplicates as dup_svc
from app.services import items as items_svc
from app.services import keys
from app.services import prds as prds_svc
from app.services import prioritization as prio
from app.services import requests as req_svc

# PRD-47 S3: collision detail shows the risk in plain language, not a reorder hint.
_COLLISION_BLOCKED = (
    "the overlap is already held, so a second claim would be refused rather than queued"
)
_COLLISION_SERIALIZE = (
    "Running them together is what produces the conflict this check exists to predict."
)
from app.services import waits as waits_svc

logger = logging.getLogger("graphban.triage")

TABS = ("all", "bugs", "requests", "blockers", "collisions")

# GRPH-1529 (P75 D5): an answered decision stays on the feed this long, saying who answered
# and whether a drain will pick its ticket up. After that it is history, in the tracker.
ANSWERED_WINDOW_SECONDS = 24 * 3600


def _aware(dt):
    return req_svc._aware(dt)


def _age(dt, now) -> int:
    dt = _aware(dt)
    return max(0, int((now - dt).total_seconds())) if dt else 0


def _source(req: Request) -> str:
    """Where the report came from. An explicit `meta.source` wins; otherwise a page URL means
    the in-app widget and its absence the public form."""
    declared = (req.meta or {}).get("source")
    if declared:
        return str(declared)
    return "In-app widget" if req.source_url else "Public form"


def _request_rows(db: Session, project_id: str, now) -> list[dict]:
    rows = []
    for req in req_svc.list_requests(db, project_id=project_id):
        if not req_svc.is_untriaged(req):
            continue
        hits = dup_svc.find_duplicates(
            db, req.title, project_id=project_id, exclude_request_id=req.id, top_k=1)
        best = hits[0] if hits else None
        duplicate = None
        if best:
            if best["kind"] == "request":
                other = db.get(Request, best["id"])
                status = other.status if other is not None else ""
            else:
                status = best.get("status", "")
            duplicate = {"id": best["id"], "title": best["title"], "score": best["score"],
                         "status": status}
        row = {
            "id": req.key, "kind": req.type, "title": req.title,
            "age_seconds": _age(req.created_at, now),
            "by": req.by, "votes": req.votes, "source": _source(req), "body": req.detail,
            "paths": [], "duplicate": duplicate,
        }
        # Omitted rather than null when nothing fits (or the embedder is down): the field is
        # advisory and its absence must not read as a verdict.
        prd = prds_svc.best_prd_for_request(db, req)
        if prd:
            row["suggested_prd"] = {"id": prd["prd_id"], "score": prd["score"]}
        rows.append(row)
    return rows


def _nobody_alerted(db: Session, item: Item) -> bool:
    """The wait (or a wait this ticket depends on) was handed off with no one to alert."""
    return item.id in _nobody_alerted_many(db, [item])


def _nobody_alerted_many(db: Session, items: list[Item]) -> set[str]:
    """`_nobody_alerted` for a whole list, in two statements whatever its length (GRPH-1901).

    One query for the list's dependency edges, one for the dependencies. Returns the ids that
    read True; an id absent from the set is the same answer as the single-item read returning
    False, which is why the caller reads membership and not a lookup that could miss.
    """
    from sqlalchemy import select

    from app.models import Link

    alerted: set[str] = set()
    need: list[str] = []
    for item in items:
        if (item.decision or {}).get("nobody_alerted"):
            alerted.add(item.id)
        else:
            need.append(item.id)
    if not need:
        return alerted

    dep_ids: dict[str, list[str]] = {iid: [] for iid in need}
    for a, b in db.execute(select(Link.a, Link.b).where(
            Link.type == "dependency", Link.a.in_(need))).all():
        if b not in dep_ids[a]:
            dep_ids[a].append(b)
    wanted = {b for ids in dep_ids.values() for b in ids}
    deps = {d.id: d for d in db.scalars(select(Item).where(Item.id.in_(wanted)))} if wanted else {}
    for iid in need:
        for dep in (deps.get(d) for d in dep_ids[iid]):
            if dep is not None and (dep.decision or {}).get("nobody_alerted") and not (
                    dep.decision or {}).get("answer"):
                alerted.add(iid)
                break
    return alerted


def _is_open_question(dep: Item) -> bool:
    """A decision wait still waiting for its answer."""
    return bool(dep.decision) and not (dep.decision or {}).get("answer") and dep.status != "done"


def _blocks(db: Session, wait: Item) -> dict:
    """The ticket that depends on this wait (GRPH-1571): first by key, plus how many more."""
    return _blocks_many(db, [wait]).get(wait.id) or {}


def _blocks_many(db: Session, waits: list[Item]) -> dict[str, dict]:
    """`_blocks` for a whole list, in two statements whatever its length (GRPH-1901).

    One query for the dependant edges of every wait, one for the tickets. Keyed by wait id; a
    wait nothing depends on is absent rather than mapped to `{}`, so `.get(id) or {}` is the
    single-wait answer and a missing key cannot read as "blocks nothing" by accident.
    """
    from sqlalchemy import select

    from app.models import Link

    ids = [w.id for w in waits]
    if not ids:
        return {}
    dependants: dict[str, list[str]] = {i: [] for i in ids}
    for b, a in db.execute(select(Link.b, Link.a).where(
            Link.type == "dependency", Link.b.in_(ids))).all():
        if a not in dependants[b]:
            dependants[b].append(a)
    wanted = {a for ticket_ids in dependants.values() for a in ticket_ids}
    tickets = ({t.id: t for t in db.scalars(select(Item).where(Item.id.in_(wanted)))}
               if wanted else {})
    out: dict[str, dict] = {}
    for wid in ids:
        mine = [t for t in (tickets.get(a) for a in dependants[wid]) if t is not None]
        if not mine:
            continue
        mine.sort(key=lambda t: (t.number or 0, t.key))
        row = {"blocks": {"id": mine[0].key, "title": mine[0].title}}
        if len(mine) > 1:
            row["blocks_more"] = len(mine) - 1
        out[wid] = row
    return out


def _blocker_rows(db: Session, project_id: str, now) -> list[dict]:
    ctx = prio.context(db, project_id)
    # `triage_blockers` renders `it.key` for items it read with the same `list_items(project_id)`
    # call `ctx` made, so a row's item is a key lookup rather than a resolve and a get of its own
    # (GRPH-1901). Holding `ctx` for the whole render is what makes that safe: the identity map
    # keeps objects weakly, and it is the render's own `item.key` reads that would otherwise
    # re-query the project every row.
    by_key = {it.key: it for it in ctx.items}
    found = [(b, by_key[b["id"]]) for b in waits_svc.triage_blockers(
        db, project_id, include_open_questions=True) if b["id"] in by_key]
    items = [it for _, it in found]
    # The three lookups a blocked row used to make on its own — its wait state, whether anyone
    # was alerted, and what it blocks — are read once for the whole list inside the batch, so the
    # statement count is what the shortest feed costs and not what the longest one does.
    with waits_svc.batched(db, items):
        states = {it.id: waits_svc.question_state(db, it) for it in items}
        alerted = _nobody_alerted_many(db, [it for it in items if states[it.id]])
        blocks = _blocks_many(db, [it for b, it in found if b.get("decision")])
        rows = []
        for b, item in found:
            # One row per question: a ticket held only by decision waits is represented by the
            # wait's row, which names it. One that also waits on something else keeps its row.
            questions: list[Item] = []
            others = 0
            if not b.get("decision"):
                for d in (ctx.item(i) for i in prio.blocked_by(ctx, item)):
                    if d is None:
                        continue
                    if _is_open_question(d):
                        questions.append(d)
                    else:
                        others += 1
                if questions and not others:
                    continue
            row = {
                "id": item.key, "kind": "blocker", "title": item.title,
                "age_seconds": _age(item.created_at, now),
                "owner": item.claimed_by, "blocked_seconds": _age(item.updated_at, now),
                "reason": item.blocker or "", "paths": list(item.touchpoints or []),
            }
            qs = states[item.id]
            if qs:
                row["question_state"] = qs
            if qs and item.id in alerted:
                row["nobody_alerted"] = True
            if questions:
                questions.sort(key=lambda q: (q.number or 0, q.key))
                row["also_questions"] = [
                    {"id": q.key, "question": (q.decision or {}).get("question") or q.title}
                    for q in questions]
            if b.get("decision"):
                row["decision"] = {"question": b["decision"]["question"],
                                   "options": b["decision"]["options"]}
                row.update(blocks.get(item.id) or {})
            else:
                deps = [d for d in (ctx.item(i) for i in prio.blocked_by(ctx, item)) if d]
                if deps:
                    # `ctx.deps` is appended in the order an unsorted Link scan returns, so
                    # "the first dependency" was whichever row the engine happened to hand
                    # back: the same graph could name a different one under another plan, on
                    # another server, or after a restored dump. Sorted the way `_blocks_many`
                    # and `questions` here already are (GRPH-1923).
                    deps.sort(key=lambda d: (d.number or 0, d.key))
                    row["waiting_on"] = {"id": deps[0].key, "title": deps[0].title,
                                         "status": deps[0].status}
            rows.append(row)
    return rows


def _answered_rows(db: Session, project_id: str, now) -> list[dict]:
    """Decision waits answered inside the window: who answered (a person, or an agent from a
    source with its quote) and the drain note for the PRD the answer unblocks."""
    from datetime import timedelta

    from sqlalchemy import select

    since = now - timedelta(seconds=ANSWERED_WINDOW_SECONDS)
    rows = []
    for wait in db.scalars(select(Item).where(
            Item.project_id == project_id, Item.status == "done",
            Item.decision.is_not(None)).order_by(Item.done_at.desc()).limit(200)):
        answer = (wait.decision or {}).get("answer")
        if not answer:
            continue
        at = _aware(wait.done_at) or now
        if at < since:
            continue
        src = answer.get("source") or None
        by = answer.get("by") or {}
        rows.append({
            "id": wait.key, "kind": "answered", "title": wait.title,
            "age_seconds": _age(at, now), "paths": [],
            "question": (wait.decision or {}).get("question") or wait.title,
            "answer": answer.get("choice") or answer.get("text") or "",
            "by": {"agent": by.get("agent"), "user": by.get("user") or None},
            "source": {"kind": src.get("kind"), "ref": src.get("ref")} if src else None,
            "quote": src.get("quote") if src else None,
            "answered_by": waits_svc.answered_by_line(answer),
            "drain": waits_svc.drain_note(db, wait),
            **_blocks(db, wait),
        })
    return rows


def _collision_rows(db: Session, project_id: str, now) -> list[dict]:
    by_id = {it.id: it for it in items_svc.list_items(db, project_id=project_id)}
    rows = []
    for c in collision_svc.triage(db, project_id):
        members = sorted((by_id[i] for i in c["items"] if i in by_id),
                         key=lambda it: (it.sort_order, it.number or 0))
        if len(members) < 2:
            continue
        # `blocked`: someone already holds a member, so the rest cannot simply be reordered.
        held = any(m.status in ("in_progress", "review") for m in members)
        risk = "blocked" if held else "serialize"
        rows.append({
            # Stable while the member set is unchanged; frozen ids, so a retag keeps it.
            "id": "col:" + collision_svc.member_key(c["items"]),
            "kind": "collision", "title": f"{len(members)} items touch the same code",
            "age_seconds": max(_age(m.created_at, now) for m in members),
            "risk": risk,
            "predicted": c["predicted"],
            "members": [{"id": m.key, "title": m.title, "owner": m.claimed_by,
                         "status": m.status} for m in members],
            "paths": c["areas"],
            "recommendation": _COLLISION_BLOCKED if held else _COLLISION_SERIALIZE,
        })
    return rows


def _tab_of(row: dict) -> set[str]:
    kind = row["kind"]
    if kind == "bug":
        return {"all", "bugs"}
    if kind in ("feature", "enhancement"):
        return {"all", "requests"}
    if kind == "blocker":
        return {"all", "blockers"}
    if kind == "collision":
        return {"all", "collisions"}
    if kind == "answered":
        return {"all", "blockers"}
    return {"all"}  # feedback: in the inbox, in no typed tab


def feed(db: Session, project_id: str) -> dict:
    now = utcnow()
    rows: list[dict] = []
    failed: list[str] = []
    for name, fn in (("requests", _request_rows), ("blockers", _blocker_rows),
                     ("collisions", _collision_rows), ("answered", _answered_rows)):
        try:
            rows.extend(fn(db, project_id, now))
        except Exception:  # noqa: BLE001 — one source failing must not hide the others
            logger.warning("triage source %s failed", name, exc_info=True)
            db.rollback()
            failed.append(name)
    # Blockers first (longest blocked first), then everything else oldest first; answered
    # decisions last, newest first — they are a receipt, not work.
    rows.sort(key=lambda r: (0, -r["blocked_seconds"]) if r["kind"] == "blocker"
              else (2, r["age_seconds"]) if r["kind"] == "answered"
              else (1, -r["age_seconds"]))
    counts = {t: 0 for t in TABS}
    for r in rows:
        if r["kind"] == "answered":
            continue  # a receipt, listed but not waiting: it adds to no tab's count
        for t in _tab_of(r):
            counts[t] += 1
    # Only Response Needed is waiting on a person; Checking context is an agent still looking.
    on_person = sum(r.get("question_state") == waits_svc.RESPONSE_NEEDED for r in rows)
    return {"rows": rows, "counts": counts, "failed_sources": failed,
            "waiting_on_person": on_person}


# ---- Console team-triage decisions (GRPH-1679) ----
#
# A manager decides in the console; the box that owns the item reads the decision on each sync,
# applies it through the same service functions the box's own triage UI calls, and reports
# `applied` or `refused` back. The box initiates both calls.
#
# The wire is the CONSOLE'S, not one this module invented (graphban-control
# `server/src/triage-routes.ts` and `docs/contract.md`, GBC-173). A served decision is
# `{id, target, decision, note, decided_at}`; `target` is an OBJECT — `{kind: "item", item_id}`
# naming a row this box published in its digest, or `{kind: "request", request_id, title, body}`
# for a public request routed here, which lands as a new item; and each result is reported by its
# own `POST .../applied` carrying ONE `{decision_id, outcome, reason}`.

DECISIONS_FEATURE_PATH = "/api/sync/triage-decisions"
# The console serves `accept` | `decline` | `defer` | `duplicate` | `assign` (GBC-277).
# `duplicate_of`, `park` and `assign_team` are the box's own older names, still applied.
# `duplicate` carries `duplicate_of: {item_id}`; `assign` carries `assignee: {id, name}`, a PERSON
# from the list `push_assignable_people` sent — not the team tag `assign_team` writes.
DECISION_TYPES = ("accept", "decline", "defer", "duplicate", "assign",
                  "duplicate_of", "park", "assign_team")
PEOPLE_FEATURE_PATH = "/api/sync/assignable-people"
APPLIED_ACTION = "triage_decision_applied"
_TIMEOUT = 30.0


class DecisionRefused(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def _author_label(decision: dict) -> str:
    a = decision.get("author")
    if isinstance(a, dict):
        return str(a.get("name") or a.get("email") or a.get("id") or "console")
    return str(a or "console")


def _decision_applied_before(db: Session, decision_id: str) -> bool:
    from sqlalchemy import select
    from app.models import Event

    for e in db.scalars(select(Event).where(Event.action == APPLIED_ACTION)):
        if (e.meta or {}).get("decision_id") == decision_id:
            return True
    return False


def _find_target(db: Session, key: str):
    rid = keys.resolve_request(db, key)
    req = db.get(Request, rid or key)
    if req is not None:
        return req
    iid = keys.resolve_item(db, key)
    return db.get(Item, iid or key)


def _target_of(decision: dict) -> tuple[str, object]:
    """Read the console's `target` OBJECT: `("item", item_id)` or `("request", the target)`.

    `("", None)` for anything else. A target that is not the console's object names nothing on
    this box: stringifying it and looking that up is what made every console decision read
    `not_found`, so an unrecognised target is refused as malformed instead.
    """
    target = decision.get("target")
    if not isinstance(target, dict):
        return "", None
    kind = str(target.get("kind") or "")
    if kind == "item":
        item_id = str(target.get("item_id") or "")
        return ("item", item_id) if item_id else ("", None)
    if kind == "request":
        return "request", target
    return "", None


def _check_unmoved(target, decision: dict) -> None:
    """The decision was made against the status the console saw. If the item moved since,
    the manager decided about something that no longer exists in that form.

    The console's decision payload carries no status — it serves `{id, target, decision, note,
    decided_at}` — so there is nothing to compare and refusing here would leave every console
    decision unapplied, which is the guarantee's opposite, not its defence. A payload that does
    carry one is still checked, which is what keeps `item_moved` meaning something.
    """
    seen = decision.get("status")
    if not seen:
        return
    if seen != target.status:
        raise DecisionRefused("item_moved", f"{target.key} is now {target.status}, was {seen}")


def _apply_one(db: Session, target, decision: dict) -> dict:
    kind = decision.get("decision")
    # The console's free text is `note`; `reason` is the box's older name for the same field.
    reason = str(decision.get("note") or decision.get("reason") or "")
    if kind not in DECISION_TYPES:
        raise DecisionRefused("unknown_decision", f"unknown decision {kind!r}")
    _check_unmoved(target, decision)
    is_req = isinstance(target, Request)
    try:
        if kind in ("accept", "park", "defer"):
            # `defer` is the console's name for the box's `park`: off the queue, not declined.
            status = "next" if kind == "accept" else "backlog"
            if is_req:
                req_svc.accept_request(db, target.id, status=status)
            else:
                items_svc.update_item(db, target.id, status=status)
        elif kind == "decline":
            if not is_req:
                raise DecisionRefused("unsupported_target", "an item cannot be declined")
            req_svc.decline_request(db, target.id, reason=reason)
        elif kind == "assign_team":
            team = str(decision.get("team") or "").strip()
            if not team:
                raise DecisionRefused("bad_decision", "assign_team needs a team")
            if is_req:
                target.meta = {**(target.meta or {}), "team": team}
                db.commit()
            else:
                tags = [t for t in (target.tags or []) if not str(t).startswith("team:")]
                items_svc.update_item(db, target.id, tags=tags + [f"team:{team}"])
        elif kind == "duplicate":
            _apply_console_duplicate(db, target, decision)
        elif kind == "assign":
            _apply_assign(db, target, decision)
        else:  # duplicate_of
            _apply_duplicate(db, target, decision)
    except req_svc.ActionRefused as e:
        db.rollback()
        raise DecisionRefused("refused", str(e))
    except ValueError as e:
        db.rollback()
        raise DecisionRefused("refused", str(e))
    return {"decision": kind}


def _apply_duplicate(db: Session, target, decision: dict) -> None:
    ref = decision.get("duplicate_of")
    remote = isinstance(ref, dict) and ref.get("deployment")
    from app.models import Link

    if remote:
        # The other item lives on another deployment: record where, not a local link.
        note = {"deployment": str(ref["deployment"]), "id": str(ref.get("id") or "")}
        if isinstance(target, Request):
            target.meta = {**(target.meta or {}), "duplicate_of": note}
            db.commit()
            req_svc.decline_request(db, target.id, reason=f"duplicate of {note['deployment']}:{note['id']}")
        else:
            db.add(Link(project_id=target.project_id, a=target.key,
                        b=f"{note['deployment']}:{note['id']}", type="semantic",
                        reason="duplicate_of (remote deployment)"))
            db.commit()
        return
    key = str(ref.get("id") if isinstance(ref, dict) else ref or "")
    other = _find_target(db, key) if key else None
    if other is None or other.project_id != target.project_id:
        raise DecisionRefused("bad_decision", f"duplicate target {key!r} not found")
    if isinstance(target, Request):
        req_svc.merge_request(db, target.id, other.id)
    else:
        db.add(Link(project_id=target.project_id, a=target.key, b=other.key,
                    type="semantic", reason="duplicate_of"))
        db.commit()


def _apply_console_duplicate(db: Session, target, decision: dict) -> None:
    """The console's `duplicate`: `duplicate_of: {item_id}` names another item THIS box holds in
    the same project. The target is linked as its duplicate; nothing else about it changes."""
    from sqlalchemy import select
    from app.models import Link

    if not isinstance(target, Item):
        raise DecisionRefused("unsupported_target", "duplicate is an item decision")
    ref = decision.get("duplicate_of")
    key = str(ref.get("item_id") or "").strip() if isinstance(ref, dict) else ""
    if not key:
        raise DecisionRefused("bad_decision", "duplicate needs duplicate_of: {item_id}")
    iid = keys.resolve_item(db, key)
    other = db.get(Item, iid or key)
    if other is None:
        raise DecisionRefused("not_found", f"duplicate_of {key!r} is not an item on this box")
    if other.project_id != target.project_id:
        raise DecisionRefused("wrong_project", f"duplicate_of {key!r} is in another project")
    if other.id == target.id:
        raise DecisionRefused("bad_decision", "an item cannot duplicate itself")
    exists = db.scalar(select(Link).where(
        Link.project_id == target.project_id, Link.a == target.key, Link.b == other.key,
        Link.reason == "duplicate_of"))
    if exists is None:
        db.add(Link(project_id=target.project_id, a=target.key, b=other.key,
                    type="semantic", reason="duplicate_of"))
        db.commit()


def assignable_people(db: Session, project_id: str) -> list:
    """The people an item on this project can be assigned to: every user who can WRITE it.

    The same rule `authz.can_write` applies to the user, so the list pushed to the console and
    the check an `assign` decision meets on the way back can never disagree.
    """
    from sqlalchemy import select
    from app.models import Membership, User
    from app.security import authz

    users = db.scalars(select(User).join(Membership, Membership.user_id == User.id).where(
        Membership.project_id == project_id, Membership.access == "write"
    ).order_by(User.name, User.id)).all()
    return [u for u in users if authz.can_write(db, u.id, project_id)]


def _apply_assign(db: Session, target, decision: dict) -> None:
    """The console's `assign`: `assignee: {id, name}`, the id one this box listed. The item's
    assignee becomes that person's handle on the project; refused, and the item untouched, when
    they cannot be assigned here (unknown, or no write access any more)."""
    from app.models import User
    from app.security import authz
    from app.services import handles

    if not isinstance(target, Item):
        raise DecisionRefused("unsupported_target", "assign is an item decision")
    who = decision.get("assignee")
    uid = str(who.get("id") or "").strip() if isinstance(who, dict) else ""
    if not uid:
        raise DecisionRefused("bad_decision", "assign needs assignee: {id, name}")
    user = db.get(User, uid)
    if user is None or not authz.can_write(db, user.id, target.project_id):
        raise DecisionRefused("not_assignable",
                              f"{uid!r} cannot be assigned on this project")
    items_svc.update_item(db, target.id,
                          assignee=handles.project_handle(db, user, target.project_id))


def push_assignable_people(db: Session, *, cloud_url: str, api_key: str) -> dict | None:
    """BOX: push each project's assignable people, `{"people": [{id, name}]}` and nothing else.

    The console replaces the whole set per project, and refuses (422) any field but `id` and
    `name` — so no email ever leaves here. A project nobody can be assigned on pushes
    `people: []`, which the console reads as "listed, nobody", not "never pushed". None when the
    console does not serve the route (404 on the first project): nothing is reported.
    """
    import httpx
    from sqlalchemy import select
    from app.models import Project

    from app.services import box_push

    url, headers = cloud_url.rstrip("/") + PEOPLE_FEATURE_PATH, {"X-API-Key": api_key}
    out: dict = {"pushed": [], "errors": []}
    # GBC-P23: `self` only once a box-push receipt has listed `assignable_self`.
    mark_self = box_push.assignable_self_seen()
    own_email = _linked_email(db) if mark_self else ""
    served = False
    for project in list(db.scalars(select(Project).order_by(Project.name, Project.id))):
        name = (project.name or "").strip()
        if not name:
            continue
        users = assignable_people(db, project.id)
        people = [{"id": u.id, "name": u.name or u.id} for u in users]
        if mark_self:
            _mark_self(people, users, own_email)
        try:
            resp = httpx.post(url, params={"project": name}, headers=headers,
                              timeout=_TIMEOUT, json={"people": people})
            if resp.status_code == 404 and not served:
                return None
            if resp.status_code in (403, 422):
                _record_people_refusal(db, project.id, resp)
            resp.raise_for_status()
            served = True
            out["pushed"].append(name)
        except Exception as e:  # noqa: BLE001 — retried on the next sync
            out["errors"].append({"project": name, "error": f"{type(e).__name__}: {e}"})
    return out


def _linked_email(db: Session) -> str:
    """The linked engineer's address (`SyncLink.linked_email`), lowercased; "" when none."""
    from app.models import SyncLink

    link = db.get(SyncLink, "instance")
    return ((link.linked_email if link is not None else "") or "").strip().lower()


def _mark_self(people: list[dict], users: list, own_email: str) -> None:
    """`self: true` on the one person whose email is the linked engineer's, else on none.

    The email decides on the box and never goes out: the entry stays `{id, name, self}`.
    """
    if not own_email:
        return
    for entry, user in zip(people, users):
        if (user.email or "").strip().lower() == own_email:
            entry["self"] = True
            return


def _record_people_refusal(db: Session, project_id: str, resp) -> None:
    from app.models import SyncProject
    from app.services import project_sync_lifecycle as lifecycle

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None
    data = data if isinstance(data, dict) else {}
    lifecycle.record_refusal(
        db, db.get(SyncProject, project_id), f"assignable-people {resp.status_code}",
        reason=str(data.get("detail") or f"http_{resp.status_code}"),
        field=data.get("field") if isinstance(data.get("field"), str) else None)


def _route_request(db: Session, target: dict, decision: dict, project_id: str | None) -> Item:
    """A public request the console routed here becomes a NEW item on the pulled project.

    The console delivers an accepted request to the box it was routed to (`route_to`) carrying
    the request's own title and body, and it applies a request `decline`/`defer` itself without
    ever serving it — so `accept` is the only decision that can arrive on this target.
    """
    if str(decision.get("decision") or "") != "accept":
        raise DecisionRefused("unsupported_target",
                              "a routed request is only ever served as accept")
    if not project_id:
        raise DecisionRefused("no_project", "a routed request needs the project it was pulled for")
    title = str(target.get("title") or "").strip()
    if not title:
        raise DecisionRefused("bad_decision", "a routed request carries no title")
    try:
        return items_svc.create_item(db, title=title, description=str(target.get("body") or ""),
                                     project_id=project_id, status="next")
    except ValueError as e:
        db.rollback()
        raise DecisionRefused("refused", str(e))


def apply_decision(db: Session, decision: dict, project_id: str | None = None) -> dict:
    """Apply one console decision. Returns the report row: `{id, result, reason?}`.

    `target` is the console's object (see `_target_of`); `project_id` is the project the decision
    was pulled for, which is where a routed request's new item lands and outside which an item
    target is refused `wrong_project` and left untouched.

    Idempotent on the decision id: a retried sync that delivers it again reports `applied`
    without touching the item a second time — or, for a routed request, without creating a
    second item.
    """
    from app.services import events as events_svc

    did = str(decision.get("id") or "")
    if not did:
        return {"id": did, "result": "refused", "reason": "bad_decision"}
    if _decision_applied_before(db, did):
        return {"id": did, "result": "applied"}
    kind, payload = _target_of(decision)
    if not kind:
        return {"id": did, "result": "refused", "reason": "bad_decision"}
    try:
        if kind == "request":
            target = _route_request(db, payload, decision, project_id)
            out = {"decision": str(decision.get("decision"))}
        else:
            target = _find_target(db, payload)
            if target is None:
                return {"id": did, "result": "refused", "reason": "not_found"}
            if project_id is not None and target.project_id != project_id:
                return {"id": did, "result": "refused", "reason": "wrong_project"}
            out = _apply_one(db, target, decision)
    except DecisionRefused as e:
        return {"id": did, "result": "refused", "reason": e.reason}
    events_svc.record(
        db, actor_type="console", actor_label=_author_label(decision), surface="rest",
        action=APPLIED_ACTION, target_type="request" if isinstance(target, Request) else "item",
        target_id=target.key, project_id=target.project_id,
        meta={"decision_id": did, "decision": out["decision"], "author": _author_label(decision)})
    return {"id": did, "result": "applied"}


def pull_decisions(db: Session, *, cloud_url: str, api_key: str) -> dict:
    """BOX: read pending decisions per project, apply each, report the results."""
    import httpx
    from sqlalchemy import select
    from app.models import Project

    base, headers = cloud_url.rstrip("/") + DECISIONS_FEATURE_PATH, {"X-API-Key": api_key}
    out = {"applied": 0, "refused": 0, "errors": []}
    served = False
    for project in list(db.scalars(select(Project).order_by(Project.name, Project.id))):
        name = (project.name or "").strip()
        if not name:
            continue
        resp = httpx.get(base, params={"project": name}, headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 404:
            if not served:
                # The first GET answers whether the console serves the feature at all.
                return {"pulled": False, "reason": "console does not serve triage decisions"}
            out["errors"].append({"project": name, "error": "404"})
            continue
        served = True
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("decisions") if isinstance(body, dict) else None
        for d in (rows or []):
            if not isinstance(d, dict):
                continue
            r = apply_decision(db, d, project.id)
            outcome = "applied" if r["result"] == "applied" else "refused"
            reason = str(r.get("reason") or "").strip()
            if outcome == "refused" and not reason:
                reason = "refused"  # the console 422s a refusal that carries no reason
            # ONE decision per call: the console answers a batch `{project, results: [...]}` with
            # 422 `decision_id is required`, so a refusal it never hears about stays pending.
            report = {"decision_id": r["id"], "outcome": outcome,
                      "reason": reason if outcome == "refused" else ""}
            try:
                httpx.post(f"{base}/applied", params={"project": name}, headers=headers,
                           timeout=_TIMEOUT, json=report).raise_for_status()
            except Exception as e:  # noqa: BLE001 — still pending on the console; retried next sync
                out["errors"].append({"project": name, "decision_id": r["id"],
                                      "error": f"{type(e).__name__}: {e}"})
            out["applied" if outcome == "applied" else "refused"] += 1
    return out
