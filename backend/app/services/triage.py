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
    from sqlalchemy import select

    from app.models import Link

    if (item.decision or {}).get("nobody_alerted"):
        return True
    for dep_id in db.scalars(select(Link.b).where(Link.type == "dependency", Link.a == item.id)):
        dep = db.get(Item, dep_id)
        if dep is not None and (dep.decision or {}).get("nobody_alerted") and not (
                dep.decision or {}).get("answer"):
            return True
    return False


def _is_open_question(dep: Item) -> bool:
    """A decision wait still waiting for its answer."""
    return bool(dep.decision) and not (dep.decision or {}).get("answer") and dep.status != "done"


def _blocks(db: Session, wait: Item) -> dict:
    """The ticket that depends on this wait (GRPH-1571): first by key, plus how many more."""
    from sqlalchemy import select

    from app.models import Link

    tickets = [t for t in (db.get(Item, i) for i in db.scalars(select(Link.a).where(
        Link.type == "dependency", Link.b == wait.id))) if t is not None]
    if not tickets:
        return {}
    tickets.sort(key=lambda t: (t.number or 0, t.key))
    out = {"blocks": {"id": tickets[0].key, "title": tickets[0].title}}
    if len(tickets) > 1:
        out["blocks_more"] = len(tickets) - 1
    return out


def _blocker_rows(db: Session, project_id: str, now) -> list[dict]:
    ctx = prio.context(db, project_id)
    rows = []
    for b in waits_svc.triage_blockers(db, project_id, include_open_questions=True):
        item = db.get(Item, keys.resolve_item(db, b["id"]) or b["id"])
        if item is None:
            continue
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
        qs = waits_svc.question_state(db, item)
        if qs:
            row["question_state"] = qs
        if qs and _nobody_alerted(db, item):
            row["nobody_alerted"] = True
        if questions:
            questions.sort(key=lambda q: (q.number or 0, q.key))
            row["also_questions"] = [
                {"id": q.key, "question": (q.decision or {}).get("question") or q.title}
                for q in questions]
        if b.get("decision"):
            row["decision"] = {"question": b["decision"]["question"],
                               "options": b["decision"]["options"]}
            row.update(_blocks(db, item))
        else:
            dep = next((d for d in (ctx.item(i) for i in prio.blocked_by(ctx, item)) if d), None)
            if dep is not None:
                row["waiting_on"] = {"id": dep.key, "title": dep.title, "status": dep.status}
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

DECISIONS_FEATURE_PATH = "/api/sync/triage-decisions"
DECISION_TYPES = ("accept", "duplicate_of", "park", "decline", "assign_team")
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


def _check_unmoved(target, decision: dict) -> None:
    """The decision was made against the status the console saw. If the item moved since,
    the manager decided about something that no longer exists in that form."""
    seen = decision.get("status")
    if not seen:
        # Without the status the console saw there is nothing to check the item against, and
        # applying blind would bypass the item_moved guarantee.
        raise DecisionRefused("no_status", "decision carries no status to check against")
    if seen != target.status:
        raise DecisionRefused("item_moved", f"{target.key} is now {target.status}, was {seen}")


def _apply_one(db: Session, target, decision: dict) -> dict:
    kind = decision.get("decision")
    reason = str(decision.get("reason") or "")
    if kind not in DECISION_TYPES:
        raise DecisionRefused("unknown_decision", f"unknown decision {kind!r}")
    _check_unmoved(target, decision)
    is_req = isinstance(target, Request)
    try:
        if kind in ("accept", "park"):
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


def apply_decision(db: Session, decision: dict, project_id: str | None = None) -> dict:
    """Apply one console decision. Returns the report row: `{id, result, reason?}`.

    Idempotent on the decision id: a retried sync that delivers it again reports `applied`
    without touching the item a second time. With `project_id` (the project the decision was
    pulled for), a target outside that project is refused `wrong_project` and left untouched.
    """
    from app.services import events as events_svc

    did = str(decision.get("id") or "")
    if not did:
        return {"id": did, "result": "refused", "reason": "bad_decision"}
    if _decision_applied_before(db, did):
        return {"id": did, "result": "applied"}
    target = _find_target(db, str(decision.get("target") or ""))
    if target is None:
        return {"id": did, "result": "refused", "reason": "not_found"}
    if project_id is not None and target.project_id != project_id:
        return {"id": did, "result": "refused", "reason": "wrong_project"}
    try:
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
        results = [apply_decision(db, d, project.id) for d in (rows or []) if isinstance(d, dict)]
        if not results:
            continue
        try:
            httpx.post(f"{base}/applied", headers=headers, timeout=_TIMEOUT,
                       json={"project": name, "results": results}).raise_for_status()
        except Exception as e:  # noqa: BLE001 — still pending on the console; retried next sync
            out["errors"].append({"project": name, "error": f"{type(e).__name__}: {e}"})
        for r in results:
            out["applied" if r["result"] == "applied" else "refused"] += 1
    return out
