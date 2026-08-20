"""Local↔cloud sync ingest (AL-137, part of the local-first hybrid AL-134).

A linked local instance builds its code graph locally (the expensive LLM describe pass runs
on the dev's machine) and pushes the *result* here in bulk. This is the cloud receiver.

Two invariants come straight from the grill:
- **Tenant-safe (D3).** The target project is resolved SERVER-SIDE from the sync credential
  (`key_sync_ids`) — never from the payload — so a push can't land in another tenant's
  workspace even if the body names a different project.
- **Re-embed, don't trust foreign vectors (D1).** The payload carries node summaries + a
  content hash, NOT embedding vectors. `describe_code` → `upsert_node` re-embeds each summary
  with the cloud's OWN embedder, so cloud search stays in one comparable vector space.

Bulk one-request ingest (not N metered MCP calls), so a full-graph push doesn't burn the
hosted call quota (D7) — locally-executed describe work never touched the cloud in the first
place.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, Project, User
from app.schemas import SyncLinkIn, SyncStatusOut
from app.security import authz
from app.security.deps import get_agent_key, get_current_user
from app.services import code_graph
from app.services import code_sync
from app.services import events as events_svc
from app.services import galaxy as galaxy_svc

router = APIRouter(prefix="/sync", tags=["sync"])


# ---- instance link + status (AL-141): the web-managed cloud link -------------------------

def _status(db: Session, user: User) -> dict:
    """Assemble the Sync/Link page payload: instance link state + per-project sync state for
    the projects this user can read, flagged with whether they can also push/purge them."""
    link = code_sync.link_status(db)
    readable = set(authz.readable_project_ids(db, user.id))
    writable = set(authz.writable_project_ids(db, user.id))
    names = {p.id: p.name for p in db.scalars(select(Project)).all() if p.id in readable}
    order = sorted(readable, key=lambda pid: names.get(pid, pid).lower())
    states = {s["project_id"]: s for s in code_sync.project_states(db, order)}
    projects = [
        {**states[pid], "name": names.get(pid, pid), "writable": pid in writable}
        for pid in order
    ]
    return {**link, "projects": projects}


@router.get("/status", response_model=SyncStatusOut)
def sync_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Link state (never the credential itself) plus per-project sync state for the page."""
    return _status(db, user)


@router.post("/link", response_model=SyncStatusOut)
def link_instance(
    body: SyncLinkIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Link this instance to a cloud tenant (AL-141). The sync key is encrypted at rest and
    overrides any env-baked link; a blank key on a re-link keeps the stored one."""
    url = body.cloud_url.strip()
    if not url:
        raise HTTPException(422, "cloud_url is required")
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    existing = code_sync.get_link(db)
    if not body.api_key and (existing is None or not existing.api_key_enc):
        raise HTTPException(422, "a sync API key is required to link")
    code_sync.set_link(db, cloud_url=url, api_key=body.api_key, org=body.org)
    events_svc.record_user(db, user, action="link_cloud_sync", target_type="instance",
                           target_id="sync_link", meta={"cloud_url": url, "org": body.org.strip()})
    return _status(db, user)


@router.delete("/link", response_model=SyncStatusOut)
def unlink_instance(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Unlink — stops all sync. Local data is untouched; cloud items remain (AL-141)."""
    code_sync.clear_link(db)
    events_svc.record_user(db, user, action="unlink_cloud_sync", target_type="instance",
                           target_id="sync_link")
    return _status(db, user)


class CodeGraphIn(BaseModel):
    """Nodes/edges in `describe_code` shape — no `project_id` (resolved from the key) and no
    embeddings (re-embedded cloud-side). `remove` marks specific paths stale (incremental
    delete); `prune` marks everything not in this batch stale (full push)."""
    nodes: list[dict] = []
    edges: list[dict] = []
    remove: list[str] = []
    prune: bool = False
    # Package names this project publishes — the registry siblings resolve against.
    # None means "this client did not look"; [] means "looked, publishes nothing".
    provides: list[str] | None = None
    # Manifest dependencies parsed by the pusher: [{"name": "@acme/core",
    # "evidence": [{"file": "web/package.json", "fact": "@acme/core ^2.1"}]}].
    # OMITTED and EMPTY differ — see `galaxy.apply_manifests`.
    manifests: list[dict] | None = None


@router.post("/code-graph")
def ingest_code_graph(
    body: CodeGraphIn,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(get_agent_key),
):
    targets = authz.key_sync_ids(db, key)
    if not targets:
        raise HTTPException(
            403,
            "this key can't sync a code graph — it needs the 'sync' scope and its owner "
            "needs write access to the target project",
        )
    project_id = targets[0]  # a sync credential is pinned to one project
    result = code_graph.describe_code(
        db, project_id=project_id, nodes=body.nodes, edges=body.edges, prune=body.prune
    )
    marked = code_graph.mark_paths_stale(db, project_id, body.remove)
    if marked:
        db.commit()
    result["marked_stale"] += marked

    # PRD-21 D3: the galaxy rides along with the code-graph push, because a manifest is
    # read from the same checkout at the same moment. Org-scoped, so it is a no-op on a
    # self-host where a project belongs to no org.
    project = db.get(Project, project_id)
    org_id = project.org_id if project is not None else None
    if org_id:
        if body.provides is not None:
            project.provides = [str(n).strip() for n in body.provides if str(n).strip()]
            db.commit()
        try:
            result["galaxy"] = galaxy_svc.apply_manifests(
                db, org_id=org_id, project_id=project_id, manifests=body.manifests
            )
        except galaxy_svc.GalaxyError as e:
            raise HTTPException(422, str(e))

    events_svc.record_key(
        db, key, action="sync_code_graph", target_type="project", target_id=project_id,
        project_id=project_id,
        meta={"nodes_upserted": result["nodes_upserted"], "edges_upserted": result["edges_upserted"],
              "marked_stale": result["marked_stale"], "prune": body.prune},
    )
    return {"project_id": project_id, **result}


@router.delete("/code-graph")
def purge_code_graph(db: Session = Depends(get_db), key: ApiKey = Depends(get_agent_key)):
    """Purge the synced code graph for the credential's project (AL-137 D8) — deletes every
    node + edge. Target resolved server-side from the sync credential, same as ingest."""
    targets = authz.key_sync_ids(db, key)
    if not targets:
        raise HTTPException(403, "this key can't purge a code graph — needs the 'sync' scope")
    project_id = targets[0]
    result = code_graph.delete_project_graph(db, project_id)
    db.commit()
    events_svc.record_key(db, key, action="purge_code_graph", target_type="project",
                          target_id=project_id, project_id=project_id, meta=result)
    return {"project_id": project_id, **result}


class PushIn(BaseModel):
    project_id: str = "core"


@router.post("/push")
def trigger_push(
    body: PushIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Push THIS (local) instance's code graph for a project up to its linked cloud tenant
    (AL-139) — the `graphban sync` trigger. Only a member who can write the project may
    sync it; a `409` means the instance isn't linked to a cloud."""
    authz.require_writable(db, user.id, body.project_id, "item")
    try:
        return code_sync.push(db, project_id=body.project_id)
    except code_sync.NotLinked as e:
        raise HTTPException(409, str(e))


@router.post("/purge")
def trigger_purge(
    body: PushIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete THIS project's code graph from its linked cloud tenant (AL-137 D8) and reset the
    local sync manifest. Write-gated; `409` when not linked."""
    authz.require_writable(db, user.id, body.project_id, "item")
    try:
        return code_sync.purge(db, project_id=body.project_id)
    except code_sync.NotLinked as e:
        raise HTTPException(409, str(e))


# ---- portable export/import (AL-140): a secondary transport with no cloud ----
_BUNDLE_VERSION = 1


@router.get("/export")
def export_code_graph(
    project_id: str = "core",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Download a project's code graph as a portable, vector-free bundle (summaries + structure).
    Air-gapped/offline alternative to the cloud sync — import it into any instance, which
    re-embeds on arrival (D1)."""
    authz.require_readable(db, user.id, project_id)
    return {"bundle_version": _BUNDLE_VERSION, "project_id": project_id,
            **code_graph.export_graph(db, project_id)}


class ImportIn(BaseModel):
    project_id: str = "core"
    nodes: list[dict] = []
    edges: list[dict] = []
    prune: bool = False


@router.post("/import")
def import_code_graph(
    body: ImportIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Import a code-graph bundle into a writable project (AL-140). Re-embeds each summary with
    THIS instance's embedder, so the imported graph is searchable in its own vector space (D1);
    the target is the caller-chosen project, gated by write access."""
    authz.require_writable(db, user.id, body.project_id, "item")
    result = code_graph.describe_code(
        db, project_id=body.project_id, nodes=body.nodes, edges=body.edges, prune=body.prune)
    events_svc.record_user(
        db, user, action="import_code_graph", target_type="project",
        target_id=body.project_id, project_id=body.project_id,
        meta={"nodes_upserted": result["nodes_upserted"], "edges_upserted": result["edges_upserted"]})
    return {"project_id": body.project_id, **result}
