"""The box publishes its specs (PRDs) to the console (GRPH-1676).

The box that holds a PRD is its home and its only author on the box side; the console
holds the published copy. On each sync, for every project whose `publish_to_org` specs
flag is on (`box_push.publish_enabled(..., "specs")`, GRPH-1674):

- a PRD whose current version (`base_hash`, the hash `get_prd` returns) differs from the
  version last published is sent with `POST /api/sync/specs`, one request per PRD, under an
  `Idempotency-Key` per (prd_id, base_hash). An unchanged PRD is not re-sent.
- a PRD that was published and is now gone — deleted, or its project turned the flag off —
  is purged with `DELETE /api/sync/specs`. Turning the flag back on finds nothing recorded
  as published, so every PRD is republished on the next sync.

What was published lives in `spec_publish` (one row per PRD, written only after the console
accepted it), so a failed call is retried on the next sync and a restart re-sends nothing.
An unlinked box publishes nothing and purges nothing: whether to purge is a question about
the org, and an unlinked box has no org to ask.
"""
from __future__ import annotations

import hashlib
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Prd, Project, SpecPublish, utcnow

logger = logging.getLogger("graphban.spec_publish")

_TIMEOUT = 30.0
_PATH = "/api/sync/specs"


def idempotency_key(prd_id: str, base_hash: str) -> str:
    """One key per (prd_id, base_hash): a resend of the same version is a replay."""
    return hashlib.sha256(f"spec:{prd_id}:{base_hash}".encode()).hexdigest()


def spec_payload(db: Session, prd: Prd, project: Project) -> dict:
    """The console's copy of one PRD version."""
    from app.services import prds as prd_svc

    grill = prd_svc.completion(db, prd.id)
    items = list(db.scalars(select(Item.id).where(Item.prd_id == prd.id).order_by(Item.id)))
    return {
        "project": (project.name or "").strip(),
        "prd_id": prd.id,
        "title": prd.title,
        "status": prd.status,
        "body_markdown": prd.body or "",
        "base_hash": prd_svc.body_hash(prd.body or ""),
        "grill": {"complete": bool(grill["complete"]),
                  "outstanding": len(grill["outstanding"])},
        "items": items,
    }


def _post(base: str, api_key: str, body: dict) -> httpx.Response:
    return httpx.post(f"{base}{_PATH}", json=body, timeout=_TIMEOUT, headers={
        "X-API-Key": api_key,
        "Idempotency-Key": idempotency_key(body["prd_id"], body["base_hash"])})


def _delete(base: str, api_key: str, row: SpecPublish) -> httpx.Response:
    return httpx.request("DELETE", f"{base}{_PATH}", timeout=_TIMEOUT,
                         headers={"X-API-Key": api_key},
                         json={"project": row.project_name, "prd_id": row.prd_id})


def _accepted(resp: httpx.Response) -> bool:
    """2xx is accepted. (A purge also takes a 404: the console already holds nothing.)"""
    return 200 <= resp.status_code < 300


def publish_specs(db: Session, *, cloud_url: str, api_key: str) -> dict:
    """Publish changed PRDs and purge withdrawn ones. One failure never stops the rest:
    it is reported, nothing is recorded for it, and the next sync tries again."""
    from app.services import box_push, code_sync
    from app.services import prds as prd_svc

    if code_sync.link_status(db)["linked"] is not True:
        return {"published": [], "purged": [], "reason": "not linked"}
    base = cloud_url.rstrip("/")
    recorded = {r.prd_id: r for r in db.scalars(select(SpecPublish))}
    wanted: dict[str, tuple[Prd, Project]] = {}
    for project in db.scalars(select(Project).order_by(Project.id)):
        if not box_push.publish_enabled(db, project.id, "specs"):
            continue
        for prd in prd_svc.list_prds(db, project.id):
            wanted[prd.id] = (prd, project)

    published: list[str] = []
    purged: list[str] = []
    failed: list[dict] = []

    for prd_id, row in sorted(recorded.items()):
        if prd_id in wanted:
            continue
        try:
            resp = _delete(base, api_key, row)
        except (httpx.HTTPError, OSError) as e:
            failed.append({"prd_id": prd_id, "op": "purge", "error": type(e).__name__})
            continue
        if _accepted(resp) or resp.status_code == 404:
            db.delete(row)
            db.commit()
            purged.append(prd_id)
        else:
            logger.warning("console refused the spec purge for %s: %s", prd_id, resp.status_code)
            failed.append({"prd_id": prd_id, "op": "purge", "status": resp.status_code})

    for prd_id, (prd, project) in sorted(wanted.items()):
        current = prd_svc.body_hash(prd.body or "")
        row = recorded.get(prd_id)
        if row is not None and row.base_hash == current:
            continue
        body = spec_payload(db, prd, project)
        try:
            resp = _post(base, api_key, body)
        except (httpx.HTTPError, OSError) as e:
            failed.append({"prd_id": prd_id, "op": "publish", "error": type(e).__name__})
            continue
        if not _accepted(resp):
            logger.warning("console refused the spec %s: %s", prd_id, resp.status_code)
            failed.append({"prd_id": prd_id, "op": "publish", "status": resp.status_code})
            continue
        if row is None:
            row = SpecPublish(prd_id=prd_id, project_id=project.id)
            db.add(row)
        row.project_id = project.id
        row.project_name = body["project"]
        row.base_hash = current
        row.published_at = utcnow()
        db.commit()
        published.append(prd_id)

    out: dict = {"published": published, "purged": purged}
    if failed:
        out["failed"] = failed
    return out
