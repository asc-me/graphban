"""The box reads specs published by other deployments and applies console edits (GRPH-1677).

The console never reaches into a box, so both arrive as records the box chooses to read, the
same pattern as org rule decisions. On each sync, per project, `GET /api/sync/specs?project=…`
returns `{"specs": [...], "revisions": [...]}`:

- `specs`: specs whose home is another deployment (`prd_id, title, home, base_hash,
  body_markdown`). They are stored in `remote_spec`, never as a `Prd`, so they are read-only
  here and `decompose` refuses them with a hint naming the home box. The set is replaced each
  pull; a spec the console stops serving is dropped.
- `revisions`: console edits to a spec THIS box is home for (`id, prd_id, base_hash,
  body_markdown, author`). `base_hash` is the version the editor saw. If it equals the PRD's
  current hash the body becomes a new PRD version attributed to the author and is reported
  `applied`; if the PRD moved since it is NOT applied and is reported `conflict` with the
  current hash; a closed PRD, or an edit that would break the baseline, is `refused`.
  Reports go to `POST /api/sync/specs/applied`. A report that failed to send is retried by the
  console re-serving the revision; one whose body already is the current body reads as `applied`.

An older console is untouched: nothing is requested unless `/api/config` advertises `specs`,
and a 404 reads as "not served" (None; the sync result carries no `specs_pull` key).
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Prd, Project, RemoteSpec, utcnow

logger = logging.getLogger("graphban.spec_pull")

_TIMEOUT = 30.0
_PATH = "/api/sync/specs"


class RemoteSpecRefused(ValueError):
    """The spec's home is another deployment; it cannot be changed or decomposed here."""


def remote_spec(db: Session, prd_id: str) -> RemoteSpec | None:
    return db.get(RemoteSpec, prd_id)


def refuse_if_remote(db: Session, prd_id: str) -> None:
    """Raise RemoteSpecRefused (hint names the home box) when `prd_id` is a spec owned elsewhere."""
    row = remote_spec(db, prd_id)
    if row is not None:
        raise RemoteSpecRefused(
            f"{prd_id} is owned by {row.home or 'another deployment'} and is read-only here. "
            f"Decompose it on {row.home or 'its home box'}, where it can be edited.")


def list_remote(db: Session, project_id: str) -> list[dict]:
    rows = db.scalars(select(RemoteSpec).where(RemoteSpec.project_id == project_id)
                      .order_by(RemoteSpec.prd_id))
    return [{"id": r.prd_id, "title": r.title, "home": r.home, "version": r.base_hash,
             "read_only": True} for r in rows]


def _apply_revision(db: Session, rev: dict) -> dict:
    from app.services import prds as prd_svc

    rid, prd_id = rev.get("id"), str(rev.get("prd_id") or "")
    out = {"id": rid, "prd_id": prd_id}
    prd = db.get(Prd, prd_id)
    if prd is None:
        return {**out, "status": "refused", "reason": "not a spec on this box"}
    body = rev.get("body_markdown")
    if not isinstance(body, str):
        return {**out, "status": "refused", "reason": "revision carries no body"}
    current = prd_svc.body_hash(prd.body or "")
    if (prd.body or "") == body:  # already applied; the report is what was lost
        return {**out, "status": "applied", "base_hash": current}
    if rev.get("base_hash") != current:
        return {**out, "status": "conflict", "base_hash": current}
    try:
        prd_svc.update_prd(db, prd.id, body=body)
    except (prd_svc.PrdClosed, prd_svc.RebaselineExpandsScope) as e:
        db.rollback()
        return {**out, "status": "refused", "reason": str(e), "base_hash": current}
    prd_svc.create_version(
        db, prd.id, note=f"Console revision by {rev.get('author') or 'a console user'}.")
    db.refresh(prd)
    return {**out, "status": "applied", "base_hash": prd_svc.body_hash(prd.body or "")}


def _store_remote(db: Session, project: Project, specs: list) -> list[str]:
    seen: set[str] = set()
    for s in specs:
        if not isinstance(s, dict) or not s.get("prd_id"):
            continue
        pid = str(s["prd_id"])
        if db.get(Prd, pid) is not None:  # ours: the box's own copy always wins
            continue
        row = db.get(RemoteSpec, pid) or RemoteSpec(prd_id=pid)
        row.project_id, row.project_name = project.id, (project.name or "").strip()
        row.title, row.home = str(s.get("title") or ""), str(s.get("home") or "")
        row.base_hash, row.body = str(s.get("base_hash") or ""), str(s.get("body_markdown") or "")
        row.pulled_at = utcnow()
        db.add(row)
        seen.add(pid)
    for row in db.scalars(select(RemoteSpec).where(RemoteSpec.project_id == project.id)):
        if row.prd_id not in seen:
            db.delete(row)
    db.commit()
    return sorted(seen)


def pull_specs(db: Session, *, cloud_url: str, api_key: str) -> dict | None:
    """Read remote specs and apply console revisions. None when the console does not serve specs."""
    from app.services import code_sync, spec_publish

    if code_sync.link_status(db)["linked"] is not True:
        return {"remote": [], "applied": [], "conflicts": [], "reason": "not linked"}
    if not spec_publish.console_serves_specs(cloud_url):
        return None
    base, headers = cloud_url.rstrip("/"), {"X-API-Key": api_key}
    remote: list[str] = []
    applied: list[str] = []
    conflicts: list[str] = []
    refused: list[str] = []
    failed: list[dict] = []
    for project in db.scalars(select(Project).order_by(Project.id)):
        name = (project.name or "").strip()
        try:
            resp = httpx.get(f"{base}{_PATH}", params={"project": name}, headers=headers,
                             timeout=_TIMEOUT)
        except (httpx.HTTPError, OSError) as e:
            failed.append({"project": name, "error": type(e).__name__})
            continue
        if resp.status_code == 404:
            return None
        if not 200 <= resp.status_code < 300:
            failed.append({"project": name, "status": resp.status_code})
            continue
        record = resp.json()
        if not isinstance(record, dict):
            continue
        remote += _store_remote(db, project, record.get("specs") or [])
        results = [_apply_revision(db, r) for r in (record.get("revisions") or [])
                   if isinstance(r, dict)]
        if not results:
            continue
        for r in results:
            {"applied": applied, "conflict": conflicts}.get(r["status"], refused).append(r["prd_id"])
        try:
            httpx.post(f"{base}{_PATH}/applied", headers=headers, timeout=_TIMEOUT,
                       json={"project": name, "results": results}).raise_for_status()
        except (httpx.HTTPError, OSError) as e:
            failed.append({"project": name, "op": "report", "error": type(e).__name__})
    out: dict = {"remote": remote, "applied": applied, "conflicts": conflicts}
    if refused:
        out["refused"] = refused
    if failed:
        out["failed"] = failed
    return out
