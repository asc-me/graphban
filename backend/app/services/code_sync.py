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

import threading
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ApiKey, CodeSyncState, Membership, OrgMembership, Organization, PlatformConfig, Project,
    SyncLink, SyncProject, Team, User, utcnow,
)
from app.security import authz, secrets
from app.services import code_graph, link_device, quotas

_BATCH = 200      # nodes per request — bounds payload size and sets resumability granularity
_TIMEOUT = 30.0
_LINK_ID = "instance"  # the singleton SyncLink row


class NotLinked(Exception):
    """No cloud sync target configured — a pure local-only instance never pushes (D2)."""


class ConsoleRefused(Exception):
    """The console answered 401/403/404: a definite no, with its reason (GRPH-1518, L3/L5)."""

    def __init__(self, reason: str, status: int = 0):
        super().__init__(reason)
        self.reason, self.status = reason, status


class ConsoleUnknown(Exception):
    """No answer we can act on: a network error, timeout, 5xx or unreadable body. Never a no."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
    _reset_org_cache()
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
    _reset_org_cache()


# ---- who may link (GRPH-1223) ------------------------------------------------------------

def box_operator_id(db: Session) -> str | None:
    """The box's first account — the operator `graphban bootstrap` or the first signup made.
    The one durable fact a self-host has about who owns the deployment: nothing else on the
    box is instance-wide (every role is a project seat, and any user may found a project)."""
    return db.scalar(select(User.id).order_by(User.created_at, User.id).limit(1))


def is_box_admin(db: Session, user: User) -> bool:
    """May this user link, re-point or unlink the whole deployment?

    Hosted: only a platform operator — a tenant must never re-point the cloud's own link.
    Self-host: the operator, or someone the operator has made owner/admin of a project the
    operator owns. "Owner of any project" is not enough: any user can create a project and
    own it, which would hand every member of the box the unlink button."""
    if settings.hosted_mode:
        return quotas.is_platform_admin(user)
    operator = box_operator_id(db)
    if operator is None or user.id == operator:
        return operator is not None
    operator_projects = select(Membership.project_id).where(
        Membership.user_id == operator, Membership.role == "owner")
    return db.scalar(
        select(Membership.project_id).where(
            Membership.user_id == user.id, Membership.role.in_(("owner", "admin")),
            Membership.project_id.in_(operator_projects),
        ).limit(1)
    ) is not None


def link_status(db: Session) -> dict:
    """Link state for the UI — the cloud URL and org label, whether a credential is set, and
    where the link comes from (db web-link vs. an env-baked link). Never leaks the key."""
    link = get_link(db)
    if link is not None:
        return {"linked": True, "source": "web", "cloud_url": link.cloud_url,
                "org": link.org, "credential_set": bool(link.api_key_enc),
                "linked_at": link.linked_at,
                "telemetry_share": bool(link.telemetry_share),
                "last_contribution_at": (link.last_contribution_at.isoformat()
                                         if link.last_contribution_at else None),
                "last_contribution_rows": link.last_contribution_rows,
                "last_floors": link.last_floors,
                "last_redacted_models": link.last_redacted_models,
                "last_snapshot_at": link.last_snapshot_at}
    if settings.sync_cloud_url and settings.sync_api_key:
        return {"linked": True, "source": "env", "cloud_url": settings.sync_cloud_url,
                "org": "", "credential_set": True, "linked_at": None,
                "telemetry_share": False, "last_contribution_at": None,
                "last_contribution_rows": None, "last_floors": None,
                "last_redacted_models": None, "last_snapshot_at": None}
    return {"linked": False, "source": "", "cloud_url": "", "org": "",
            "credential_set": False, "linked_at": None,
            "telemetry_share": False, "last_contribution_at": None,
            "last_contribution_rows": None, "last_floors": None,
            "last_redacted_models": None, "last_snapshot_at": None}


def compute_diff(local: dict[str, str], pushed: dict[str, str]) -> tuple[list[str], list[str]]:
    """(changed, removed): paths whose hash is new or differs from the last push, and paths
    that were pushed before but are gone locally now."""
    changed = sorted(p for p, h in local.items() if pushed.get(p) != h)
    removed = sorted(p for p in pushed if p not in local)
    return changed, removed


def _node_payload(n) -> dict:
    return {"path": n.path, "kind": n.kind, "name": n.name, "lang": n.lang,
            "summary": n.summary, "content_hash": n.content_hash}


def _reason(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        found = data.get("reason") or data.get("error") or data.get("detail")
        if isinstance(found, str) and found:
            return found
    return f"http_{resp.status_code}"


def _classify(resp: httpx.Response) -> httpx.Response:
    """Three states, never two: 401/403/404 is `ConsoleRefused` (with the console's reason),
    5xx is `ConsoleUnknown`; any other status is returned for the caller to read."""
    if resp.status_code >= 500:
        raise ConsoleUnknown(f"the console answered {resp.status_code}")
    if resp.status_code in (401, 403, 404):
        raise ConsoleRefused(_reason(resp), resp.status_code)
    return resp


def _send(method: str, url: str, headers: dict, *, body: dict | None = None,
          timeout: float = _TIMEOUT) -> httpx.Response:
    try:
        if method == "GET":
            resp = httpx.get(url, headers=headers, timeout=timeout)
        else:
            resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
    except (httpx.HTTPError, OSError) as e:
        raise ConsoleUnknown(f"could not reach {url.split('/api/')[0]}: {e.__class__.__name__}")
    return _classify(resp)


def _post(url: str, api_key: str, body: dict, headers: dict | None = None) -> None:
    resp = _send("POST", f"{url.rstrip('/')}/api/sync/code-graph",
                 headers or {"X-API-Key": api_key}, body=body)
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
         batch_size: int = _BATCH, headers: dict | None = None) -> dict:
    """Push the local code graph for `project_id` to the linked cloud tenant, incrementally.
    `headers` replaces the `X-API-Key` auth (a deployment token push, `push_mapped`); the
    key-only rule/box tail is skipped then, since it speaks the key's protocol."""
    if headers is not None:
        url, key = cloud_url, ""
    else:
        url, key = _target(db, cloud_url, api_key)
    tail = (lambda: {}) if headers is not None else (lambda: _sync_tail(db, url, key))

    # Privacy (D8): a project can opt out of ever pushing its graph off-network.
    cfg = db.get(PlatformConfig, project_id)
    if cfg is not None and not cfg.sync_graph:
        return {"project_id": project_id, "skipped": True, "reason": "graph kept local (sync_graph off)",
                "pushed": 0, "edges": 0, **tail()}

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
        _post(url, key, {"nodes": [_node_payload(nodes[p]) for p in chunk]}, headers)
        for p in chunk:
            pushed[p] = local[p]
        state.manifest = dict(pushed)     # persist progress BEFORE the next batch → resumable
        state.last_synced_at = utcnow()
        db.commit()
        sent += len(chunk)

    if removed:
        _post(url, key, {"remove": removed}, headers)
        for p in removed:
            pushed.pop(p, None)
        state.manifest = dict(pushed)
        state.last_synced_at = utcnow()
        db.commit()

    # Edges are lightweight and idempotent on the cloud (upsert_edge skips dupes); push the
    # current set whenever nodes moved. Edge-level incrementality is a follow-up.
    edges_sent = 0
    if changed or removed:
        edges = code_graph.list_edges(db, project_id)
        if edges:
            _post(url, key, {"edges": [{"src": e.src, "dst": e.dst, "type": e.type} for e in edges]},
                  headers)
            edges_sent = len(edges)

    return {"project_id": project_id, "pushed": sent, "removed": len(removed),
            "unchanged": len(local) - len(changed), "edges": edges_sent,
            **tail()}


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


# ---- project mapping and per-project push (GRPH-1518, P67 §6 L3/L4/L5) -------------------

def _console_auth(db: Session) -> tuple[str, dict]:
    """The console URL and auth headers for a deployment link: the token, else a migrated
    key (`legacy_key`). Raises `NotLinked` when the box holds neither."""
    link = db.get(SyncLink, _LINK_ID)
    if link is not None:
        base = (link.console_url or link.cloud_url or "").rstrip("/")
        token = secrets.decrypt(link.token_enc) if link.token_enc else ""
        if base and token:
            return base, {"Authorization": f"Bearer {token}"}
        key = secrets.decrypt(link.legacy_key) if link.legacy_key else ""
        if base and key:
            return base, {"X-API-Key": key}
    raise NotLinked("this box is not signed in to a console: start with POST /api/sync/link/start")


def console_projects(db: Session) -> dict:
    """The cloud projects the linked engineer can reach, read live with the token:
    `ok {projects}` (possibly empty), `refused {reason}` or `unknown {reason}`. An
    unreachable console is never an empty `ok`."""
    base, headers = _console_auth(db)
    try:
        resp = _send("GET", f"{base}/api/sync/projects", headers, timeout=_ORG_TIMEOUT)
        try:
            data = resp.json()
        except ValueError:
            data = None
        rows = data.get("projects") if isinstance(data, dict) else data
        if resp.status_code != 200 or not isinstance(rows, list):
            raise ConsoleUnknown(f"the console answered {resp.status_code} without a project list")
    except ConsoleRefused as e:
        return {"status": "refused", "reason": e.reason}
    except ConsoleUnknown as e:
        return {"status": "unknown", "reason": e.reason}
    projects = [{"id": str(r["id"]), "name": str(r.get("name") or r["id"])}
                for r in rows if isinstance(r, dict) and r.get("id")]
    return {"status": "ok", "projects": projects}


def set_project_map(db: Session, mappings: list[tuple[str, str]]) -> None:
    """Replace the local → cloud mapping. A project left out is local only. A kept row whose
    cloud project is unchanged keeps its last result; a re-pointed one is pending again."""
    existing = link_device.mappings(db)
    wanted = {local: cloud for local, cloud in mappings}
    for local, row in existing.items():
        if local not in wanted:
            db.delete(row)
    for local, cloud in wanted.items():
        row = existing.get(local)
        if row is None:
            db.add(SyncProject(local_project_id=local, cloud_project_id=cloud))
        elif row.cloud_project_id != cloud:
            row.cloud_project_id = cloud
            row.last_push_at = row.last_push_result = row.last_push_reason = None
    db.commit()


def _store_result(row: SyncProject, result: str, reason: str | None) -> None:
    row.last_push_at, row.last_push_result, row.last_push_reason = utcnow(), result, reason


def push_mapped(db: Session) -> list[dict]:
    """Push every mapped project and return a result per project (box admin's trigger).

    `done {nodes, edges}` / `refused {reason}` / `unknown {reason}`, each stored on its
    `sync_project` row. One project's refusal never stops the others. A 401 `seat_removed` or
    `token_revoked` is the credential's, not the project's: the whole link is refused, so
    every mapped project is stored refused with that reason (L5). Mappings are never touched
    here, so after a takeover each one is re-checked by this next push."""
    base, auth = _console_auth(db)
    results: list[dict] = []
    rows = sorted(link_device.mappings(db).values(), key=lambda m: m.local_project_id)
    for i, row in enumerate(rows):
        headers = {**auth, "X-Cloud-Project": row.cloud_project_id} if row.cloud_project_id else auth
        try:
            out = push(db, project_id=row.local_project_id, cloud_url=base, headers=headers)
            _store_result(row, "ok", None)
            results.append({"project_id": row.local_project_id, "status": "done",
                            "nodes": out.get("pushed", 0), "edges": out.get("edges", 0),
                            "at": row.last_push_at})
        except ConsoleRefused as e:
            db.rollback()
            if e.reason in link_device.LINK_REFUSALS:
                for r in rows:
                    _store_result(r, "refused", e.reason)
                db.commit()
                return [{"project_id": r.local_project_id, "status": "refused",
                         "reason": e.reason, "at": r.last_push_at} for r in rows]
            _store_result(row, "refused", e.reason)
            db.commit()
            results.append({"project_id": row.local_project_id, "status": "refused",
                            "reason": e.reason, "at": row.last_push_at})
        except ConsoleUnknown as e:
            db.rollback()
            _store_result(row, "unknown", e.reason)
            db.commit()
            results.append({"project_id": row.local_project_id, "status": "unknown",
                            "reason": e.reason, "at": row.last_push_at})
        else:
            db.commit()
    return results


# ---- org overview (GRPH-1223): the Organization window ----------------------------------

_ORG_TTL = 30.0          # seconds a fetched overview is served without asking the cloud again
_ORG_TIMEOUT = 3.0       # the window must not hang on a cloud that is down
_ORG_GITOPS_COLUMNS = (
    "gitops_base_branch", "gitops_no_push_to_base", "gitops_branch_name_pattern",
    "gitops_pr_title_pattern", "gitops_reviewer_bar", "gitops_version_scheme",
    "gitops_release_defined_in", "gitops_merge_method", "gitops_model",
)

# Process-local, keyed on the link it was fetched through so a re-link never serves the old
# org. `last` survives a failed fetch: it is the "last known identity" the window keeps
# showing while `unreachable_since` says how long it has been stale.
_org_lock = threading.Lock()
_org_cache: dict = {}


def _reset_org_cache() -> None:
    with _org_lock:
        _org_cache.clear()


def cloud_org_overview(db: Session, key: ApiKey) -> dict | None:
    """CLOUD side: the org a sync credential pushes into, as the linked box's window shows it.

    The org is resolved from the credential's project, never from the request (the same
    tenant-safety as ingest). None when the credential's project belongs to no org — a
    cloud that is itself a self-host has nothing to show. Deployments group sync keys by
    name, because one key is one deployment and its name is the only identity stored."""
    from app.services import galaxy as galaxy_svc  # galaxy imports items → avoid a cycle

    targets = authz.key_sync_ids(db, key)
    project = db.get(Project, targets[0]) if targets else None
    org = db.get(Organization, project.org_id) if project is not None and project.org_id else None
    if org is None:
        return None

    from app.services import rules_org  # rules_org imports rule_toggles → keep lazy

    published = rules_org.published_decisions(db, org.id)
    groups: dict[str, dict] = {}
    for d in galaxy_svc.deployments(db, org.id):
        if d["revoked"]:
            continue
        g = groups.setdefault(d["label"], {"name": d["label"], "projects": set(),
                                           "here": False, "last_sync_at": None,
                                           "rules": None})
        if g["rules"] is None:
            g["rules"] = rules_org.box_rule_state(db, d["credential_id"], published)
        g["projects"].add(d["project_id"])
        g["here"] = g["here"] or d["credential_id"] == key.id
        if d["last_push_at"] and (g["last_sync_at"] is None or d["last_push_at"] > g["last_sync_at"]):
            g["last_sync_at"] = d["last_push_at"]
    deployments = [{**g, "projects": len(g["projects"])} for g in groups.values()]

    count = lambda model: db.scalar(  # noqa: E731
        select(func.count()).select_from(model).where(model.org_id == org.id)) or 0
    return {
        "org": {"name": org.name, "host": org.public_host, "plan": org.plan},
        "role": authz.org_role(db, key.user_id, org.id),
        "counts": {
            "members": count(OrgMembership),
            "teams": count(Team),
            "policy_rules": sum(getattr(org, c) is not None for c in _ORG_GITOPS_COLUMNS),
            "deployments": len(deployments),
        },
        "deployments": deployments,
    }


def fetch_org_overview(url: str, key: str) -> dict:
    """GET {cloud}/api/sync/org-overview. Raises `ConsoleRefused` (401/403/404, with the
    console's reason) or `ConsoleUnknown` (network error, timeout, 5xx, unreadable answer);
    a failure is no longer one undifferentiated None (GRPH-1518). `org_overview` is the
    caller that decides what an unreachable cloud looks like."""
    resp = _send("GET", f"{url.rstrip('/')}/api/sync/org-overview", {"X-API-Key": key},
                 timeout=_ORG_TIMEOUT)
    try:
        body = resp.json() if resp.status_code == 200 else None
    except ValueError:
        body = None
    if not (isinstance(body, dict) and isinstance(body.get("org"), dict)):
        raise ConsoleUnknown(f"the console answered {resp.status_code} without an overview")
    return body


def _local_last_sync(db: Session) -> datetime | None:
    return db.scalar(select(func.max(CodeSyncState.last_synced_at)))


def org_overview(db: Session) -> dict:
    """BOX side: the Organization window. Unlinked is `{"linked": False}` and nothing else.

    Fetched from the linked tenant and cached `_ORG_TTL` seconds. On a failed fetch:
    `reachable: False`, `unreachable_since` (the first failure, held until a fetch succeeds),
    the last known identity and role, and **null counts** — the last numbers are not
    presented as current. Deployments keep their last known names with null figures, except
    this one's `last_sync_at`, which is read from this box's own sync state."""
    status = link_status(db)
    if not status["linked"]:
        return {"linked": False}
    creds = cloud_credentials(db)
    if creds is None:  # a web link whose key could not be decrypted
        return {"linked": False}
    url, key = creds
    ident = (url, str(status.get("linked_at") or ""))
    now = utcnow()

    with _org_lock:
        cached = dict(_org_cache) if _org_cache.get("ident") == ident else {}
    fresh = (cached.get("ok") and cached.get("fetched_at")
             and (now - cached["fetched_at"]).total_seconds() < _ORG_TTL)
    if fresh:
        body = cached["last"]
    else:
        try:
            body = fetch_org_overview(url, key)
        except (ConsoleRefused, ConsoleUnknown):
            body = None
        with _org_lock:
            if _org_cache.get("ident") != ident:
                _org_cache.clear()
                _org_cache["ident"] = ident
            if body is not None:
                _org_cache.update(ok=True, last=body, fetched_at=now, unreachable_since=None)
            else:
                _org_cache["ok"] = False
                _org_cache["unreachable_since"] = _org_cache.get("unreachable_since") or now
            cached = dict(_org_cache)

    if body is not None:
        return {"linked": True, "reachable": True, "unreachable_since": None,
                "fetched_at": cached.get("fetched_at"), "org": body.get("org"),
                "role": body.get("role"), "counts": body.get("counts"),
                "deployments": body.get("deployments") or []}

    last = cached.get("last") or {}
    here_sync = _local_last_sync(db)
    deployments = [
        {"name": d.get("name") or "", "projects": None, "here": bool(d.get("here")),
         "last_sync_at": here_sync if d.get("here") else None}
        for d in (last.get("deployments") or [])
    ]
    return {"linked": True, "reachable": False,
            "unreachable_since": cached.get("unreachable_since"),
            "fetched_at": cached.get("fetched_at"),
            "org": last.get("org") or {"name": status.get("org") or "", "host": None, "plan": None},
            "role": last.get("role"), "counts": None, "deployments": deployments}


# ---- pulling the org's rule decisions (GRPH-1297) ----------------------------------------

def _push_box_on_sync(db: Session, url: str, key: str) -> dict:
    """The org-view push rides this same sync, once, after the graph batches. A refusal or a
    network error is reported here and retried on the next sync; neither fails the graph push."""
    from app.services import box_push

    try:
        return box_push.push_box_view(db, cloud_url=url, api_key=key)
    except Exception as e:  # noqa: BLE001 — the graph push already landed; never undo it
        db.rollback()
        return {"pushed": False, "error": f"{type(e).__name__}: {e}"}


def _sync_tail(db: Session, url: str, key: str) -> dict:
    """Rule decisions, then the org-view push. Both run after any graph batches."""
    return {
        "rules": _pull_rules_on_sync(db, url, key),
        "box": _push_box_on_sync(db, url, key),
    }


def _pull_rules_on_sync(db: Session, url: str, key: str) -> dict:
    """Every sync (`graphban sync`, `POST /api/sync/push`) also pulls the org's rule decisions —
    that is the box's sync cadence. Never fatal to the push: a failure is reported in the
    result and the next sync tries again."""
    try:
        return pull_rule_decisions(db, cloud_url=url, api_key=key)
    except Exception as e:  # noqa: BLE001 — the graph push already landed; never undo it
        db.rollback()
        return {"pulled": False, "error": f"{type(e).__name__}: {e}"}


def pull_rule_decisions(db: Session, *, cloud_url: str = "", api_key: str = "") -> dict:
    """BOX: pull the org's published rule decisions, apply them, then report the version
    applied. The box initiates both calls; the cloud never reaches in. The report is retried
    on the next pull if it failed, so the cloud never shows a version the box did not apply."""
    from app.models import RuleDecisionPull
    from app.services import rules_org

    url, key = _target(db, cloud_url, api_key)
    base, headers = url.rstrip("/"), {"X-API-Key": key}
    resp = httpx.get(f"{base}/api/sync/rule-decisions", headers=headers, timeout=_TIMEOUT)
    if resp.status_code == 404:
        return {"pulled": False, "reason": "no organization for this credential"}
    resp.raise_for_status()
    record = resp.json()
    # Nothing published yet: version is null, not "0". Do not apply and do not report.
    if not isinstance(record, dict) or record.get("version") is None:
        return {"pulled": False, "reason": "nothing published"}
    pull = db.get(RuleDecisionPull, "instance")
    changed = pull is None or pull.applied_version != record.get("version")
    if changed:
        pull = rules_org.apply_decisions(db, record)
    try:
        httpx.post(f"{base}/api/sync/rule-decisions/applied", headers=headers, timeout=_TIMEOUT,
                   json={"org_id": pull.org_id, "version": pull.applied_version}
                   ).raise_for_status()
        pull.reported = True
        db.commit()
    except (httpx.HTTPError, OSError):
        pass  # reported stays False; the next pull reports it
    return {"pulled": True, "version": pull.applied_version, "changed": changed,
            "reported": pull.reported}
