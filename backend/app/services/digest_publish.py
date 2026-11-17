"""Item and request digests for team triage (GRPH-1678).

A digest, not a copy: id, kind, title, status, item_kind, touchpoints, created_at and the
box's own duplicate/overlap hints. Never a description, body, comment or memory text.
One `POST /api/sync/item-digests` per project per sync replaces this deployment's previous
set wholesale. Switching the flag off sends `DELETE`; switching it on republishes.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Project, Request, utcnow

logger = logging.getLogger("graphban.digest_publish")

_TIMEOUT = 30.0
DONE_WINDOW = timedelta(days=14)
OPEN_REQUEST_STATUSES = ("new", "triaging", "snoozed")

#: Project ids whose digest this process has purged since it last published them.
_purged: set[str] = set()


def reset() -> None:
    _purged.clear()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _aware(dt):
    from datetime import timezone
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _item_rows(db: Session, project_id: str) -> list[dict]:
    from app.services import clustering, items as items_svc

    cutoff = utcnow() - DONE_WINDOW
    pool = clustering.load_pool(db, project_id)
    rows = []
    for item in pool:
        if item.status == "done":
            done = _aware(item.done_at)
            if done is None or done < cutoff:
                continue
        hints = []
        for rel in clustering.related_items(db, item, project_id, pool)[:5]:
            other = rel["item"]
            if rel["shared"]:
                hints.append({"type": "overlap", "id": other.key,
                              "evidence": {"shared_touchpoints": rel["shared"]}})
            elif "dependency" in rel["link_types"]:
                hints.append({"type": "dependency", "id": other.key,
                              "evidence": {"link_types": rel["link_types"]}})
        rows.append({
            "id": item.key, "kind": "item", "title": item.title, "status": item.status,
            "item_kind": item.kind or "", "touchpoints": list(item.touchpoints or []),
            "created_at": _iso(item.created_at), "hints": hints,
        })
    return rows


def _request_rows(db: Session, project_id: str) -> list[dict]:
    from app.services import duplicates as dup_svc

    rows = []
    reqs = db.scalars(select(Request).where(
        Request.project_id == project_id, Request.status.in_(OPEN_REQUEST_STATUSES)
    ).order_by(Request.created_at)).all()
    for req in reqs:
        hints = []
        for hit in dup_svc.find_duplicates(db, req.title, project_id=project_id,
                                           exclude_request_id=req.id, top_k=1):
            other = db.get(Request if hit["kind"] == "request" else Item, hit["id"])
            hints.append({"type": "duplicate", "id": getattr(other, "key", hit["id"]),
                          "evidence": {"matched_kind": hit["kind"], "score": hit["score"]}})
        rows.append({
            "id": req.key, "kind": "request", "title": req.title, "status": req.status,
            "item_kind": req.type, "touchpoints": [],
            "created_at": _iso(req.created_at), "hints": hints,
        })
    return rows


def build_digest(db: Session, project_id: str) -> list[dict]:
    return _item_rows(db, project_id) + _request_rows(db, project_id)


def publish_digests(db: Session, *, cloud_url: str, api_key: str) -> dict:
    """Publish or purge each project's digest. A failure on one project does not stop the rest."""
    from app.services import box_push

    base = f"{cloud_url.rstrip('/')}/api/sync/item-digests"
    headers = {"X-API-Key": api_key}
    out: dict = {"published": [], "purged": [], "errors": []}
    for project in list(db.scalars(select(Project).order_by(Project.name, Project.id))):
        name = (project.name or "").strip()
        if not name:
            continue
        try:
            if box_push.publish_enabled(db, project.id, "item_digests"):
                body = {"record": "item_digests", "project": name,
                        "items": build_digest(db, project.id)}
                httpx.post(base, json=body, headers=headers,
                           timeout=_TIMEOUT).raise_for_status()
                _purged.discard(project.id)
                out["published"].append(name)
            elif project.id not in _purged:
                resp = httpx.request("DELETE", base, params={"project": name},
                                     headers=headers, timeout=_TIMEOUT)
                if resp.status_code != 404:
                    resp.raise_for_status()
                _purged.add(project.id)
                out["purged"].append(name)
        except Exception as e:  # noqa: BLE001 — retried on the next sync
            db.rollback()
            logger.warning("digest publish failed for %s: %s", name, e)
            out["errors"].append({"project": name, "error": f"{type(e).__name__}: {e}"})
    return out
