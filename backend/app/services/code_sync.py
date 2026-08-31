"""Local→cloud code-graph push (AL-139) — the client side of the sync (cloud receiver is
AL-137, `POST /api/sync/code-graph`).

Incremental by content hash: only nodes whose describe output changed since the last confirmed
push ship. The last-pushed manifest ({path: content_hash}) lives in `CodeSyncState` and is
updated **per confirmed batch**, so an interrupted push resumes without re-sending confirmed
work (the resumability guarantee, D4). Paths removed locally are pruned on the cloud (the
staleness guard). Vectors never leave the box — the payload is summaries + hashes; the cloud
re-embeds (D1).
"""
from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CodeSyncState, PlatformConfig, SyncLink, utcnow
from app.security import secrets
from app.services import code_graph

_BATCH = 200      # nodes per request — bounds payload size and sets resumability granularity
_TIMEOUT = 30.0
_LINK_ID = "instance"  # the singleton SyncLink row


class NotLinked(Exception):
    """No cloud sync target configured — a pure local-only instance never pushes (D2)."""


# ---- instance-wide cloud link (AL-141): the web-managed link + status --------------------

def get_link(db: Session) -> SyncLink | None:
    """The instance link row, or None when the box has never been linked. A row whose
    `cloud_url` is blank counts as unlinked."""
    link = db.get(SyncLink, _LINK_ID)
    return link if (link and link.cloud_url) else None


def set_link(db: Session, *, cloud_url: str, api_key: str, org: str = "") -> SyncLink:
    """Link this instance to a cloud tenant. The sync key is stored encrypted at rest and
    never read back out to the UI; a blank `api_key` on a re-link keeps the stored one so the
    redacted round-trip from the form can't wipe it. Overrides env SYNC_CLOUD_URL/KEY."""
    link = db.get(SyncLink, _LINK_ID)
    if link is None:
        link = SyncLink(id=_LINK_ID)
        db.add(link)
    link.cloud_url = cloud_url.strip().rstrip("/")
    if api_key:
        link.api_key_enc = secrets.encrypt(api_key)
    link.org = org.strip()
    link.linked_at = utcnow()
    db.commit()
    db.refresh(link)
    return link


def clear_link(db: Session) -> None:
    """Unlink — stops all sync. Local data (graph, manifests) is untouched; the row is
    blanked so a later re-link starts clean."""
    link = db.get(SyncLink, _LINK_ID)
    if link is not None:
        link.cloud_url = ""
        link.api_key_enc = ""
        link.org = ""
        link.linked_at = None
        db.commit()


def link_status(db: Session) -> dict:
    """Link state for the UI — the cloud URL and org label, whether a credential is set, and
    where the link comes from (db web-link vs. an env-baked link). Never leaks the key."""
    link = get_link(db)
    if link is not None:
        return {"linked": True, "source": "web", "cloud_url": link.cloud_url,
                "org": link.org, "credential_set": bool(link.api_key_enc),
                "linked_at": link.linked_at}
    if settings.sync_cloud_url and settings.sync_api_key:
        return {"linked": True, "source": "env", "cloud_url": settings.sync_cloud_url,
                "org": "", "credential_set": True, "linked_at": None}
    return {"linked": False, "source": "", "cloud_url": "", "org": "",
            "credential_set": False, "linked_at": None}


def compute_diff(local: dict[str, str], pushed: dict[str, str]) -> tuple[list[str], list[str]]:
    """(changed, removed): paths whose hash is new or differs from the last push, and paths
    that were pushed before but are gone locally now."""
    changed = sorted(p for p, h in local.items() if pushed.get(p) != h)
    removed = sorted(p for p in pushed if p not in local)
    return changed, removed


def _node_payload(n) -> dict:
    return {"path": n.path, "kind": n.kind, "name": n.name, "lang": n.lang,
            "summary": n.summary, "content_hash": n.content_hash}


def _post(url: str, api_key: str, body: dict) -> None:
    resp = httpx.post(f"{url.rstrip('/')}/api/sync/code-graph", json=body,
                      headers={"X-API-Key": api_key}, timeout=_TIMEOUT)
    resp.raise_for_status()


def _target(db: Session, cloud_url: str, api_key: str) -> tuple[str, str]:
    """Resolve the cloud target, most-specific first: explicit args (the CLI passes its
    config-file creds) → the web-managed DB link → the env link. A pure local-only instance
    matches none and never pushes (D2)."""
    if cloud_url and api_key:
        return cloud_url, api_key
    link = get_link(db)
    if link is not None:
        key = secrets.decrypt(link.api_key_enc)
        if link.cloud_url and key:
            return link.cloud_url, key
    if settings.sync_cloud_url and settings.sync_api_key:
        return settings.sync_cloud_url, settings.sync_api_key
    raise NotLinked("no cloud sync target configured — link an instance or set SYNC_CLOUD_URL / SYNC_API_KEY")


def cloud_credentials(db: Session) -> tuple[str, str] | None:
    """The link+env cloud URL and key, or None when this box has no cloud target."""
    try:
        return _target(db, "", "")
    except NotLinked:
        return None


def push(db: Session, *, project_id: str, cloud_url: str = "", api_key: str = "",
         batch_size: int = _BATCH) -> dict:
    """Push the local code graph for `project_id` to the linked cloud tenant, incrementally."""
    url, key = _target(db, cloud_url, api_key)

    # Privacy (D8): a project can opt out of ever pushing its graph off-network.
    cfg = db.get(PlatformConfig, project_id)
    if cfg is not None and not cfg.sync_graph:
        return {"project_id": project_id, "skipped": True, "reason": "graph kept local (sync_graph off)"}

    nodes = {n.path: n for n in code_graph.list_nodes(db, project_id)}
    local = {p: (n.content_hash or "") for p, n in nodes.items()}

    state = db.get(CodeSyncState, project_id)
    if state is None:
        state = CodeSyncState(project_id=project_id, manifest={})
        db.add(state)
    pushed = dict(state.manifest or {})

    changed, removed = compute_diff(local, pushed)

    sent = 0
    for i in range(0, len(changed), batch_size):
        chunk = changed[i:i + batch_size]
        _post(url, key, {"nodes": [_node_payload(nodes[p]) for p in chunk]})
        for p in chunk:
            pushed[p] = local[p]
        state.manifest = dict(pushed)     # persist progress BEFORE the next batch → resumable
        state.last_synced_at = utcnow()
        db.commit()
        sent += len(chunk)

    if removed:
        _post(url, key, {"remove": removed})
        for p in removed:
            pushed.pop(p, None)
        state.manifest = dict(pushed)
        state.last_synced_at = utcnow()
        db.commit()

    # Edges are lightweight and idempotent on the cloud (upsert_edge skips dupes); push the
    # current set whenever nodes moved. Edge-level incrementality is a follow-up.
    if changed or removed:
        edges = code_graph.list_edges(db, project_id)
        if edges:
            _post(url, key, {"edges": [{"src": e.src, "dst": e.dst, "type": e.type} for e in edges]})

    return {"project_id": project_id, "pushed": sent, "removed": len(removed),
            "unchanged": len(local) - len(changed)}


def purge(db: Session, *, project_id: str, cloud_url: str = "", api_key: str = "") -> dict:
    """Delete this project's code graph FROM the cloud (AL-137 D8) and reset the local sync
    manifest, so a later re-enable does a clean full re-push."""
    url, key = _target(db, cloud_url, api_key)
    resp = httpx.request("DELETE", f"{url.rstrip('/')}/api/sync/code-graph",
                         headers={"X-API-Key": key}, timeout=_TIMEOUT)
    resp.raise_for_status()
    state = db.get(CodeSyncState, project_id)
    if state is not None:
        state.manifest = {}
        state.last_synced_at = utcnow()
        db.commit()
    return resp.json()


def project_states(db: Session, project_ids: list[str]) -> list[dict]:
    """Per-project sync state for the Sync/Link page: total local nodes, how many the cloud
    last confirmed, how many are pending (changed/new since the last push), the last sync
    time, whether the project opted out of pushing (`sync_graph`), and a rolled-up status.

    All derived from local truth — the manifest + current node hashes — so this reflects
    exactly what the next push would send without calling the cloud."""
    out: list[dict] = []
    for pid in project_ids:
        nodes = code_graph.list_nodes(db, pid)
        local = {n.path: (n.content_hash or "") for n in nodes}
        state = db.get(CodeSyncState, pid)
        pushed = dict(state.manifest or {}) if state is not None else {}
        changed, removed = compute_diff(local, pushed)
        pending = len(changed) + len(removed)
        cfg = db.get(PlatformConfig, pid)
        sync_graph = cfg.sync_graph if cfg is not None else True

        if not sync_graph:
            status = "paused"
        elif not local:
            status = "empty"
        elif pending == 0 and pushed:
            status = "live"
        elif pushed or (state is not None and state.last_synced_at):
            status = "stale"
        else:
            status = "unsynced"

        out.append({
            "project_id": pid,
            "sync_graph": sync_graph,
            "total_nodes": len(local),
            "synced_nodes": len(pushed),
            "pending": pending,
            "last_synced_at": state.last_synced_at if state is not None else None,
            "status": status,
        })
    return out
