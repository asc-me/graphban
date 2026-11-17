"""Item and request digests for team triage (GRPH-1678).

A digest, not a copy: id, kind, title, status, item_kind, touchpoints, created_at and the
box's own duplicate/overlap hints. Never a description, body, comment or memory text.
One `POST /api/sync/item-digests` per project per sync replaces this deployment's previous
set wholesale. Switching the flag off sends `DELETE`; switching it on republishes.

The digest lists every non-done item and every open request, and nothing else: an item that
closes drops out of the next set. A console that predates digests does not advertise
`item_digests` on its `/api/config`; such a console is not sent anything, so a box linked to
it syncs exactly as before.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Project, Request

logger = logging.getLogger("graphban.digest_publish")

_TIMEOUT = 30.0
#: The feature a console advertises on `/api/config` when it serves `/api/sync/item-digests`.
DIGESTS_FEATURE = "item_digests"
OPEN_REQUEST_STATUSES = ("new", "triaging", "snoozed")
#: How many hints of each sort a row carries.
MAX_HINTS = 5


class NotServed(Exception):
    """The console answered 404 for the digest endpoint: it does not serve digests."""


#: Project ids whose digest this process has purged since it last published them.
_purged: set[str] = set()


def reset() -> None:
    _purged.clear()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def console_serves_digests(cloud_url: str) -> bool:
    """Has the console advertised `item_digests`? Anything else (an older console, an error,
    a body that is not JSON) is False, and nothing is sent."""
    from app.services import box_push

    try:
        resp = httpx.get(f"{cloud_url.rstrip('/')}/api/config", timeout=box_push._PROBE_TIMEOUT)
        if getattr(resp, "status_code", None) != 200:
            return False
        return box_push._advertises(resp.json(), DIGESTS_FEATURE)
    except Exception:  # noqa: BLE001 — a probe failure means "not served", never an error
        return False


def _item_rows(db: Session, project_id: str) -> list[dict]:
    from app.services import clustering

    pool = clustering.load_pool(db, project_id)
    rows = []
    for item in pool:
        if item.status == "done":
            continue
        hints = []
        for rel in clustering.related_items(db, item, project_id, pool):
            other = rel["item"]
            if other.status == "done":
                continue
            if rel["shared"]:
                hints.append({"type": "overlap", "id": other.key,
                              "evidence": {"shared_touchpoints": rel["shared"]}})
            elif "dependency" in rel["link_types"]:
                hints.append({"type": "dependency", "id": other.key,
                              "evidence": {"link_types": rel["link_types"]}})
            if len(hints) >= MAX_HINTS:
                break
        rows.append({
            "id": item.key, "kind": "item", "title": item.title, "status": item.status,
            "item_kind": item.kind or "", "touchpoints": list(item.touchpoints or []),
            "created_at": _iso(item.created_at), "hints": hints,
        })
    return rows


def _embed_all(texts: list[str]) -> list[list[float]]:
    """Every text embedded exactly once: one batch call when the provider has `embed_many`."""
    from app.providers import get_embedder

    if not texts:
        return []
    embedder = get_embedder()
    batch = getattr(embedder, "embed_many", None)
    if callable(batch):
        return list(batch(texts))
    return [embedder.embed(t) for t in texts]


def _request_rows(db: Session, project_id: str) -> list[dict]:
    """Open requests, each with its likeliest duplicate among the open work in the digest.

    Each open request and each non-done item is embedded once per sync and compared in
    memory: R+I embedder calls (one batch where the provider has one), not R x (R+I).
    """
    from app.providers import cosine_similarity
    from app.services import duplicates as dup_svc

    reqs = db.scalars(select(Request).where(
        Request.project_id == project_id, Request.status.in_(OPEN_REQUEST_STATUSES)
    ).order_by(Request.created_at)).all()
    if not reqs:
        return []
    items = db.scalars(select(Item).where(
        Item.project_id == project_id, Item.status != "done")).all()
    # Same texts duplicates.find_duplicates compares; the vectors never leave the box.
    vecs = _embed_all([r.title for r in reqs]
                      + [f"{i.title} {i.description}" for i in items])
    req_vecs, item_vecs = vecs[:len(reqs)], vecs[len(reqs):]
    candidates = ([("request", r, v) for r, v in zip(reqs, req_vecs)]
                  + [("item", i, v) for i, v in zip(items, item_vecs)])
    rows = []
    for req, qv in zip(reqs, req_vecs):
        best = None
        for kind, other, ov in candidates:
            if other is req:
                continue
            score = cosine_similarity(qv, ov)
            if score >= dup_svc.DEFAULT_THRESHOLD and (best is None or score > best[0]):
                best = (score, kind, other)
        hints = []
        if best is not None:
            score, kind, other = best
            hints.append({"type": "duplicate", "id": other.key,
                          "evidence": {"matched_kind": kind, "score": round(score, 4)}})
        rows.append({
            "id": req.key, "kind": "request", "title": req.title, "status": req.status,
            "item_kind": req.type, "touchpoints": [],
            "created_at": _iso(req.created_at), "hints": hints,
        })
    return rows


def build_digest(db: Session, project_id: str) -> list[dict]:
    return _item_rows(db, project_id) + _request_rows(db, project_id)


def publish_digests(db: Session, *, cloud_url: str, api_key: str) -> dict | None:
    """Publish or purge each project's digest. A failure on one project does not stop the rest.

    Returns None when the console does not serve digests: it did not advertise `item_digests`,
    or answered the endpoint with a 404. Nothing is reported and nothing is retried as an
    error. A box talking to an older console syncs exactly as it did before.
    """
    from app.services import box_push

    if not console_serves_digests(cloud_url):
        return None
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
                resp = httpx.post(base, json=body, headers=headers, timeout=_TIMEOUT)
                if resp.status_code == 404:
                    raise NotServed()
                resp.raise_for_status()
                _purged.discard(project.id)
                out["published"].append(name)
            elif project.id not in _purged:
                resp = httpx.request("DELETE", base, params={"project": name},
                                     headers=headers, timeout=_TIMEOUT)
                if resp.status_code == 404:
                    raise NotServed()
                resp.raise_for_status()
                _purged.add(project.id)
                out["purged"].append(name)
        except NotServed:
            # Advertised, then not served (a console rolled back): no error, nothing sent.
            db.rollback()
            return None
        except Exception as e:  # noqa: BLE001 — retried on the next sync
            db.rollback()
            logger.warning("digest publish failed for %s: %s", name, e)
            out["errors"].append({"project": name, "error": f"{type(e).__name__}: {e}"})
    return out
