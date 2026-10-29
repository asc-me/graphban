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
from app.services import waits as waits_svc

logger = logging.getLogger("graphban.triage")

TABS = ("all", "bugs", "requests", "blockers", "collisions")


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


def _blocker_rows(db: Session, project_id: str, now) -> list[dict]:
    ctx = prio.context(db, project_id)
    rows = []
    for b in waits_svc.triage_blockers(db, project_id):
        item = db.get(Item, keys.resolve_item(db, b["id"]) or b["id"])
        if item is None:
            continue
        row = {
            "id": item.key, "kind": "blocker", "title": item.title,
            "age_seconds": _age(item.created_at, now),
            "owner": item.claimed_by, "blocked_seconds": _age(item.updated_at, now),
            "reason": item.blocker or "", "paths": list(item.touchpoints or []),
        }
        if b.get("decision"):
            row["decision"] = {"question": b["decision"]["question"],
                               "options": b["decision"]["options"]}
        else:
            dep = next((d for d in (ctx.item(i) for i in prio.blocked_by(ctx, item)) if d), None)
            if dep is not None:
                row["waiting_on"] = {"id": dep.key, "title": dep.title, "status": dep.status}
        rows.append(row)
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
        order = " → ".join(m.key for m in members)
        rows.append({
            # Stable while the member set is unchanged; frozen ids, so a retag keeps it.
            "id": "col:" + collision_svc.member_key(c["items"]),
            "kind": "collision", "title": f"{len(members)} items touch the same code",
            "age_seconds": max(_age(m.created_at, now) for m in members),
            "risk": "blocked" if held else "serialize",
            "predicted": c["predicted"],
            "members": [{"id": m.key, "title": m.title, "owner": m.claimed_by,
                         "status": m.status} for m in members],
            "paths": c["areas"],
            "recommendation": (f"Finish {members[0].key} first, then {order[len(members[0].key) + 3:]}"
                               if not held else
                               f"A member is already in flight; hold the rest behind it: {order}"),
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
    return {"all"}  # feedback: in the inbox, in no typed tab


def feed(db: Session, project_id: str) -> dict:
    now = utcnow()
    rows: list[dict] = []
    failed: list[str] = []
    for name, fn in (("requests", _request_rows), ("blockers", _blocker_rows),
                     ("collisions", _collision_rows)):
        try:
            rows.extend(fn(db, project_id, now))
        except Exception:  # noqa: BLE001 — one source failing must not hide the others
            logger.warning("triage source %s failed", name, exc_info=True)
            db.rollback()
            failed.append(name)
    # Blockers first (longest blocked first), then everything else oldest first.
    rows.sort(key=lambda r: (0, -r["blocked_seconds"]) if r["kind"] == "blocker"
              else (1, -r["age_seconds"]))
    counts = {t: 0 for t in TABS}
    for r in rows:
        for t in _tab_of(r):
            counts[t] += 1
    return {"rows": rows, "counts": counts, "failed_sources": failed}
