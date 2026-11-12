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

from app.config import settings
from app.db import get_db
from app.errors import Refused
from app.models import ApiKey, Project, SyncLink, User
from app.schemas import GitopsView, SyncLinkIn, SyncOrgOverviewOut, SyncStatusOut
from app.security import authz
from app.security.deps import get_agent_key, get_current_user
from app.services import code_graph
from app.services import code_sync
from app.services import events as events_svc
from app.services import galaxy as galaxy_svc
from app.services import gitops
from app.services import link_device

router = APIRouter(prefix="/sync", tags=["sync"])


# ---- instance link + status (AL-141): the web-managed cloud link -------------------------

def _status(db: Session, user: User) -> dict:
    """Assemble the Sync/Link page payload: instance link state + per-project sync state for
    the projects this user can read, flagged with whether they can also push/purge them."""
    link = code_sync.link_status(db)
    maps = link_device.mappings(db)
    deployment = link_device.link_view(db, maps)
    link["credential_set"] = link["credential_set"] or deployment["signed_in"]
    readable = set(authz.readable_project_ids(db, user.id))
    writable = set(authz.writable_project_ids(db, user.id))
    names = {p.id: p.name for p in db.scalars(select(Project)).all() if p.id in readable}
    order = sorted(readable, key=lambda pid: names.get(pid, pid).lower())
    states = {s["project_id"]: s for s in code_sync.project_states(db, order)}
    projects = [
        {**states[pid], **link_device.project_view(maps.get(pid)),
         "name": names.get(pid, pid), "writable": pid in writable}
        for pid in order
    ]
    return {**link, **deployment, "projects": projects,
            "can_link": code_sync.is_box_admin(db, user),
            "console_url_env": settings.sync_cloud_url or ""}


@router.get("/status", response_model=SyncStatusOut)
def sync_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Link state (never the credential itself) plus per-project sync state for the page."""
    return _status(db, user)


LINK_RULE = "sync.link_needs_box_admin"


def refuse_unless_box_admin(db: Session, user: User, action: str) -> None:
    """Linking, re-pointing and unlinking act on the whole deployment, so they need a box
    admin (GRPH-1223) — not merely a signed-in member. See `code_sync.is_box_admin`."""
    if not code_sync.is_box_admin(db, user):
        raise Refused(
            403, f"only an admin of this box can {action} it to a cloud org",
            rule_id=LINK_RULE,
            hint="ask the box's operator, or an owner/admin of one of their projects",
            target=("instance", "sync_link"),
        )


# ---- signing a deployment in (GRPH-1517, P67 §6 L2): relays the console's device code ----

class LinkStartIn(BaseModel):
    label: str = ""
    #: The wizard's step 1. Ignored when `SYNC_CLOUD_URL` bakes the console in.
    console_url: str = ""


class LinkStartOut(BaseModel):
    """`started` carries the code to show; `refused`/`unknown` carry the reason. Never the
    device code, the token or the key."""
    status: str  # started | refused | unknown
    console_url: str | None = None
    user_code: str | None = None
    verification_url: str | None = None
    expires_at: str | None = None
    interval: int | None = None
    reason: str | None = None


class LinkPollOut(BaseModel):
    """Exactly one of `approved {org, email}`, `pending`, `refused {reason}`, `unknown {reason}`."""
    status: str
    org: str | None = None
    email: str | None = None
    reason: str | None = None


@router.post("/link/start", response_model=LinkStartOut, response_model_exclude_none=True)
def link_start(body: LinkStartIn, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """Ask the console for a device code to sign this deployment in (box admin). When the
    link is a migrated key link, the console is sent the key's public prefix so approval
    revokes it (L6). Stores only the sign-in in flight."""
    refuse_unless_box_admin(db, user, "sign in")
    try:
        return link_device.start(db, label=body.label, requested_console=body.console_url)
    except link_device.NoConsole as e:
        raise HTTPException(422, str(e))


@router.get("/link/poll", response_model=LinkPollOut, response_model_exclude_none=True)
def link_poll(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The console's answer to the sign-in in flight (box admin). Only `approved` writes
    anything; a network error, timeout or 5xx is `unknown`, never `refused`. `404` when no
    sign-in is in flight (nothing to poll — not a policy refusal)."""
    refuse_unless_box_admin(db, user, "sign in")
    try:
        out = link_device.poll(db)
    except link_device.NoSignIn as e:
        raise HTTPException(404, str(e))
    if out["status"] == "approved":
        code_sync._reset_org_cache()  # the org overview now reads a different link
        events_svc.record_user(db, user, action="sign_in_cloud_sync", target_type="instance",
                               target_id="sync_link",
                               meta={"org": out["org"], "email": out["email"]})
    return out


@router.get("/org", response_model=SyncOrgOverviewOut, response_model_exclude_unset=True)
def org_overview(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The Organization window (GRPH-1223): the linked org's identity, the linking
    credential's org role, member/team/policy/deployment counts and every deployment, read
    from the cloud and cached briefly. Unreachable is `reachable: false` with
    `unreachable_since` and null counts; unlinked is `linked: false` and nothing else.
    `can_unlink` is whether the VIEWER is a box admin — `role` is the linking credential's,
    not theirs, so the window cannot infer it (GRPH-1225)."""
    out = code_sync.org_overview(db)
    if out["linked"]:
        out["can_unlink"] = code_sync.is_box_admin(db, user)
    return SyncOrgOverviewOut(**out)


@router.get("/org-overview")
def cloud_org_overview(db: Session = Depends(get_db), key: ApiKey = Depends(get_agent_key)):
    """CLOUD side of `GET /sync/org`: the org this sync credential pushes into. The org is
    resolved from the credential, never the request. 404 when the credential is not a sync
    key or its project belongs to no org — either way there is no org it can see."""
    out = code_sync.cloud_org_overview(db, key)
    if out is None:
        raise HTTPException(404, "no organization for this credential (needs a 'sync' key "
                                 "on an org project)")
    return out


def _linked(fn, *args, **kwargs):
    """Run a box-side sync call; `409` when this box is not linked to a cloud — the one answer
    every sync trigger (push, purge, pull-rules) gives for that state."""
    try:
        return fn(*args, **kwargs)
    except code_sync.NotLinked as e:
        raise HTTPException(409, str(e))


def _key_org(db: Session, key: ApiKey):
    targets = authz.key_sync_ids(db, key)
    project = db.get(Project, targets[0]) if targets else None
    org_id = project.org_id if project is not None else None
    if not org_id:
        raise HTTPException(404, "no organization for this credential (needs a 'sync' key "
                                 "on an org project)")
    return org_id


@router.get("/rule-decisions")
def cloud_rule_decisions(db: Session = Depends(get_db), key: ApiKey = Depends(get_agent_key)):
    """CLOUD: the org's rule decisions as a versioned record, readable on the box's sync
    credential (GRPH-1297). The box pulls it; nothing reaches into the box."""
    from app.services import rules_org

    out = rules_org.published_decisions(db, _key_org(db, key))
    out["changed_at"] = out["changed_at"].isoformat() if out["changed_at"] else None
    return out


class RuleDecisionApplied(BaseModel):
    org_id: str
    version: str


@router.post("/rule-decisions/applied")
def cloud_rule_decisions_applied(body: RuleDecisionApplied, db: Session = Depends(get_db),
                                 key: ApiKey = Depends(get_agent_key)):
    """CLOUD: the box reports the version it applied (the only source of "last synced")."""
    from app.services import rules_org

    org_id = _key_org(db, key)
    if body.org_id != org_id:
        raise HTTPException(404, "no such organization for this credential")
    row = rules_org.record_box_applied(db, key, org_id, body.version)
    return {"applied_version": row.applied_version, "applied_at": row.applied_at}


@router.post("/pull-rules")
def trigger_pull_rules(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """BOX: pull and apply the org's rule decisions now, without waiting for the next sync
    (every `POST /push` pulls them too). Box admin only; `409` when not linked, as `/push`."""
    refuse_unless_box_admin(db, user, "pull the org's rule decisions")
    return _linked(code_sync.pull_rule_decisions, db)


@router.post("/link", response_model=SyncStatusOut)
def link_instance(
    body: SyncLinkIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Link this instance to a cloud tenant (AL-141). The sync key is encrypted at rest and
    overrides any env-baked link; a blank key on a re-link keeps the stored one. Box admins
    only (GRPH-1223)."""
    refuse_unless_box_admin(db, user, "link")
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
    """Unlink — stops all sync. Local data is untouched; cloud items remain (AL-141). Box
    admins only (GRPH-1223)."""
    refuse_unless_box_admin(db, user, "unlink")
    link_device.clear(db)
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
    # Where this deployment says it can be reached. Self-reported and advisory — the cloud
    # has no way to verify it and never tries (PRD-21 D6).
    base_url: str | None = None


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
    # GET /sync/gitops uses the same isolation and resolve_local ONLY: never resolve(),
    # so a box whose SYNC_CLOUD_URL points at itself cannot recurse.
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
    if body.base_url is not None:
        # One key is one deployment, so the credential is where its address lives.
        key.base_url = (body.base_url or "").strip()
        db.commit()

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


@router.get("/gitops", response_model=GitopsView)
def sync_gitops(db: Session = Depends(get_db), key: ApiKey = Depends(get_agent_key)):
    targets = authz.key_sync_ids(db, key)
    if not targets:
        raise HTTPException(403, "this key can't sync gitops — it needs the 'sync' scope")
    # Isolation: project from the key, never a query-string id. resolve_local only —
    # never resolve() / never outbound (see comment next to ingest key_sync_ids).
    return gitops.resolve_local(db, targets[0])


class PushIn(BaseModel):
    project_id: str | None = None


class ProjectMapping(BaseModel):
    local_project_id: str
    cloud_project_id: str = ""
    #: The cloud project's tag from the reachable-projects answer (GRPH-1562); optional,
    #: and corrected by the next projects read.
    cloud_project_tag: str = ""


class ProjectMapIn(BaseModel):
    mappings: list[ProjectMapping]


def _mapping_view(db: Session) -> list[dict]:
    return [{"local_project_id": pid, **link_device.project_view(m)}
            for pid, m in sorted(link_device.mappings(db).items())]


@router.get("/projects")
def list_cloud_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The cloud projects the linked engineer can reach, read live from the console (box
    admin): `ok {projects}` (possibly empty), `refused {reason}` or `unknown {reason}`, never
    an empty list for an unreachable console. `mapping` is the stored local → cloud map."""
    refuse_unless_box_admin(db, user, "map projects of")
    return {**_linked(code_sync.console_projects, db), "mapping": _mapping_view(db)}


@router.put("/projects")
def put_project_map(body: ProjectMapIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Store the local → cloud project mapping (box admin), replacing the old one: a project
    left out is local only. Stores nothing about access; the console answers that per push."""
    refuse_unless_box_admin(db, user, "map projects of")
    link = db.get(SyncLink, "instance")
    pairs = {m.local_project_id: m.cloud_project_id.strip() for m in body.mappings}
    known = set(db.scalars(select(Project.id).where(Project.id.in_(list(pairs)))).all())
    for local, cloud in pairs.items():
        if local not in known:
            raise HTTPException(404, f"no local project {local!r}")
        if not cloud and not (link and link.legacy_key):
            raise HTTPException(422, f"project {local!r} needs a cloud_project_id")
    tags = {m.cloud_project_id.strip(): m.cloud_project_tag.strip() for m in body.mappings}
    code_sync.set_project_map(db, list(pairs.items()), tags)
    return {"mapping": _mapping_view(db)}


@router.post("/push")
def trigger_push(
    body: PushIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Push THIS (local) instance's code graph up to its linked cloud (AL-139) — the
    `graphban sync` trigger.

    With a `project_id`: that one project, as before — a member who can write it. With none
    (GRPH-1518, box admin): every mapped project, returning a result PER PROJECT — `done
    {nodes, edges}`, `refused {reason}` or `unknown {reason}` — each stored on its mapping.
    `409` means the instance isn't linked to a cloud."""
    if body is not None and body.project_id:
        authz.require_writable(db, user.id, body.project_id, "item")
        return _linked(code_sync.push, db, project_id=body.project_id)
    refuse_unless_box_admin(db, user, "push")
    return {"projects": _linked(code_sync.push_mapped, db)}


@router.post("/purge")
def trigger_purge(
    body: PushIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete THIS project's code graph from its linked cloud tenant (AL-137 D8) and reset the
    local sync manifest. Write-gated; `409` when not linked."""
    authz.require_writable(db, user.id, body.project_id, "item")
    return _linked(code_sync.purge, db, project_id=body.project_id)


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
