"""The super galaxy: evidenced dependencies between projects in one org (PRD-21 D3).

One rule shapes this whole module: **an edge between repos must name the file that proves
it.** Nothing here is inferred from embedding similarity, shared vocabulary or summary
overlap — two repos that both describe "authentication" are not related; two repos where
one's lockfile names the other are.

The client sends *facts*, not edges. It cannot know what other projects exist in the org,
so it pushes the manifest dependency names it parsed and the server resolves each against
``Project.provides`` within the pushing key's org.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from datetime import timezone

from sqlalchemy import func

from app.models import (
    Agent, ApiKey, CodeNode, Event, Item, Organization, Project, ProjectEdge, utcnow,
)
from app.security import authz
from app.services import items as items_svc
from app.services import quotas

KINDS = ("depends_on", "serves", "declared")


class GalaxyError(ValueError):
    """A push the caller can fix — the router turns it into a 422."""


def name_registry(db: Session, org_id: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Map every published package name in this org to the project publishing it.

    Returns ``(resolvable, collisions)``. A name claimed by two projects lands in
    ``collisions`` and is **excluded from the map**: an ambiguous name is a coin flip, and
    guessing is the one thing this graph must never do. The collision is reported rather
    than silently resolved to whichever row came back first.
    """
    claims: dict[str, list[str]] = {}
    for project in db.scalars(select(Project).where(Project.org_id == org_id)):
        for raw in project.provides or []:
            name = str(raw).strip()
            if name:
                claims.setdefault(name, []).append(project.id)

    resolvable = {n: ids[0] for n, ids in claims.items() if len(ids) == 1}
    collisions = {n: ids for n, ids in claims.items() if len(ids) > 1}
    return resolvable, collisions


def _validate(dep: dict) -> tuple[str, list[dict]]:
    name = str(dep.get("name") or "").strip()
    if not name:
        raise GalaxyError("each manifest dependency needs a 'name'")
    evidence = dep.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        raise GalaxyError(
            f"dependency {name!r} has no evidence — an edge that cannot name the file "
            "proving it is a guess, and this graph does not draw guesses"
        )
    for entry in evidence:
        if not isinstance(entry, dict) or not str(entry.get("file") or "").strip():
            raise GalaxyError(f"dependency {name!r} has an evidence entry with no 'file'")
    return name, evidence


def apply_manifests(
    db: Session,
    *,
    org_id: str,
    project_id: str,
    manifests: list[dict] | None,
) -> dict:
    """Resolve one project's declared dependencies into edges. Commits.

    ``manifests`` carries the load-bearing distinction on this wire format:

    - **omitted** (``None``) — an older client that did not look. **Stales nothing.**
    - **present but empty** (``[]``) — looked, found none. **Stales this project's edges.**

    Collapsing those two would write absence-reads-as-clean permanently into a protocol,
    where it is far harder to dig out than a bad test: every old client would silently
    delete the dependency graph of every project it pushed.

    Unresolved names are dropped per-edge — an npm or PyPI package is not a repo in this
    org — but they are **counted and returned**, because a silent drop with no count is
    the failure this codebase keeps finding.
    """
    if manifests is None:
        return {
            "looked": False,
            "edges_upserted": 0,
            "edges_marked_stale": 0,
            "resolved": 0,
            "external": 0,
            "external_names": [],
            "collisions": {},
        }

    resolvable, collisions = name_registry(db, org_id)
    existing = {
        (e.dst_project_id, e.kind): e
        for e in db.scalars(
            select(ProjectEdge).where(
                ProjectEdge.src_project_id == project_id,
                ProjectEdge.kind == "depends_on",
            )
        )
    }

    seen: set[tuple[str, str]] = set()
    external: list[str] = []
    upserted = 0

    for dep in manifests:
        name, evidence = _validate(dep)
        target = resolvable.get(name)
        if target is None:
            # Either an external package or a name two projects claim. Both are dropped;
            # only the collision is separately reportable, so the count stays honest.
            external.append(name)
            continue
        if target == project_id:
            # A monorepo depending on a name it publishes itself. The galaxy's resolution
            # is the checkout, so this is internal structure and belongs in its code graph.
            continue

        key = (target, "depends_on")
        seen.add(key)
        edge = existing.get(key)
        if edge is None:
            edge = ProjectEdge(
                id="pe_" + uuid.uuid4().hex[:12],
                org_id=org_id,
                src_project_id=project_id,
                dst_project_id=target,
                kind="depends_on",
            )
            db.add(edge)
        edge.resolved_name = name
        edge.evidence = evidence
        edge.fresh = True
        edge.updated_at = utcnow()
        upserted += 1

    # An edge this push no longer declares goes stale — it is not deleted, and its evidence
    # is not trimmed. A stale edge with its evidence removed would be worse than a deleted
    # one: a relationship with no explanation.
    marked = 0
    for key, edge in existing.items():
        if key not in seen and edge.fresh:
            edge.fresh = False
            edge.updated_at = utcnow()
            marked += 1

    db.commit()
    return {
        "looked": True,
        "edges_upserted": upserted,
        "edges_marked_stale": marked,
        "resolved": len(seen),
        "external": len(external),
        "external_names": sorted(set(external))[:50],
        "collisions": {n: ids for n, ids in collisions.items() if n in set(external)},
    }


def galaxy(db: Session, org_id: str) -> dict:
    """Nodes and edges for one org's galaxy.

    Node weight is code-graph node count and edge weight is the number of currently-fresh
    evidence entries — both counts of real rows, so a repo that dropped a dependency
    cannot keep a fat edge.

    Stale edges are **included and flagged**, never filtered out. The caller renders them
    faint; removing them here would make "this dependency went away" and "there was never
    a dependency" arrive identically.
    """
    projects = list(db.scalars(select(Project).where(Project.org_id == org_id)))
    ids = {p.id for p in projects}

    nodes = []
    for p in projects:
        described = db.scalars(
            select(CodeNode.id).where(CodeNode.project_id == p.id, CodeNode.fresh.is_(True))
        ).all()
        nodes.append({
            "id": p.id,
            "tag": p.tag,
            "name": p.name,
            "accent": p.accent,
            "provides": list(p.provides or []),
            "node_count": len(described),
            # False means no deployment has pushed a graph for this project yet — the org
            # is not empty, this project's structure simply has not arrived.
            "pushed": len(described) > 0,
        })

    edges = []
    for e in db.scalars(select(ProjectEdge).where(ProjectEdge.org_id == org_id)):
        if e.src_project_id not in ids or e.dst_project_id not in ids:
            continue
        edges.append({
            "id": e.id,
            "src": e.src_project_id,
            "dst": e.dst_project_id,
            "kind": e.kind,
            "resolved_name": e.resolved_name,
            "evidence": list(e.evidence or []),
            "weight": len(e.evidence or []),
            "fresh": e.fresh,
            "reason": e.reason,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        })

    _, collisions = name_registry(db, org_id)
    return {
        "nodes": nodes,
        "edges": edges,
        # Names two projects both claim. Reported rather than resolved: these draw no edge
        # at all, and a galaxy missing an edge with no explanation is the worse outcome.
        "collisions": [
            {"name": n, "project_ids": pids} for n, pids in sorted(collisions.items())
        ],
    }


def outbound_stubs(db: Session, project_id: str) -> list[dict]:
    """Dependencies that leave this repo, ready to draw on its own code graph (D4).

    The payoff for D3 being strict. Because every edge had to name the file that proves
    it, the project-level view can anchor each arrow on the **real node** for that file —
    `web/package.json` — instead of floating it somewhere plausible. No new edge rows and
    no migration on a hot table: this is `ProjectEdge.evidence` read from a second angle.

    ``anchor_paths`` is the intersection of the evidence files with this project's
    described nodes, and ``unanchored`` says when that intersection is empty. A manifest
    can name a file the code graph has never described, and an arrow drawn from nowhere
    with no explanation would be indistinguishable from one drawn from a guess — the thing
    D3 exists to prevent. So the stub still renders, and it says why it is floating.
    """
    project = db.get(Project, project_id)
    if project is None or not project.org_id:
        return []

    described = {
        path for (path,) in db.execute(
            select(CodeNode.path).where(
                CodeNode.project_id == project_id, CodeNode.fresh.is_(True)
            )
        )
    }

    out: list[dict] = []
    for edge in db.scalars(
        select(ProjectEdge).where(ProjectEdge.src_project_id == project_id)
    ):
        target = db.get(Project, edge.dst_project_id)
        if target is None:
            continue
        files = [str(e.get("file") or "") for e in (edge.evidence or [])]
        anchors = [f for f in files if f in described]
        out.append({
            "edge_id": edge.id,
            "project_id": target.id,
            "tag": target.tag,
            "name": target.name,
            "accent": target.accent,
            "kind": edge.kind,
            "resolved_name": edge.resolved_name,
            "fresh": edge.fresh,
            "evidence": list(edge.evidence or []),
            "anchor_paths": anchors,
            # True when the declaring file exists in the manifest but not in this
            # project's described graph — the arrow is real, its anchor is missing.
            "unanchored": not anchors,
        })
    return out


def deployments(db: Session, org_id: str) -> list[dict]:
    """The local boxes pushing into this tenant (PRD-21 D6).

    Every field here is **already cloud-held**, which is the finding that removes the hard
    part of this screen. When a box is linked, only the code-graph tools run locally;
    claims, leases, heartbeats and enrolments are forwarded, so `Agent` and
    `AreaReservation` are cloud rows. "Which agents are running, on what" is a query, not
    an embed — and nothing here reaches into the box.

    One key is one deployment. The cloud stores no other deployment identity, so the sync
    credential's **name** is the label, which is what makes naming it at mint time
    load-bearing.
    """
    projects = {
        p.id: p for p in db.scalars(select(Project).where(Project.org_id == org_id))
    }
    if not projects:
        return []

    out: list[dict] = []
    for key in db.scalars(
        select(ApiKey).where(ApiKey.project_id.in_(projects.keys())).order_by(ApiKey.created_at)
    ):
        if "sync" not in (key.scopes or []):
            continue
        project = projects[key.project_id]

        last_push = db.scalar(
            select(func.max(Event.ts)).where(
                Event.action == "sync_code_graph", Event.project_id == project.id
            )
        )
        node_count = db.scalar(
            select(func.count()).select_from(CodeNode).where(
                CodeNode.project_id == project.id, CodeNode.fresh.is_(True)
            )
        ) or 0

        # Agents currently working this project — forwarded up by the linked box itself.
        agents = [
            {"key": a.key, "label": a.label, "role": a.active_role, "state": a.state}
            for a in db.scalars(select(Agent).where(Agent.project_id == project.id))
            if not a.dismissed
        ]

        out.append({
            # The key's name IS the deployment's label; there is no other identity stored.
            "label": key.name,
            "credential_id": key.id,
            "prefix": key.prefix,
            "project_id": project.id,
            "project_tag": project.tag,
            "project_name": project.name,
            "base_url": key.base_url,
            "last_push_at": last_push.isoformat() if last_push else None,
            "node_count": node_count,
            # `never` is not `stale`. A credential that has never pushed is a link that was
            # set up and not finished; one that pushed a month ago is a box that stopped.
            # They call for different actions, so they are different words.
            "freshness": "never" if last_push is None else _freshness(last_push),
            "revoked": key.revoked,
            "agents": agents,
        })
    return out


def _freshness(last_push) -> str:
    """`in_sync` while a push is recent, `stale` once it is not. Evaluated on read — there
    is no sweeper, so a deployment cannot be marked stale by a job that failed to run."""
    ts = last_push if last_push.tzinfo else last_push.replace(tzinfo=timezone.utc)
    age = (utcnow() - ts).total_seconds()
    return "in_sync" if age < 24 * 3600 else "stale"


def overview(db: Session, org_id: str, user_id: str) -> dict:
    """Every project in the org at once — the first cross-project aggregate (PRD-21 D2).

    §3.3 established that no org-scoped aggregate existed anywhere and that one could not
    be obtained by relaxing a filter: `authz.require_readable` fails closed on a null
    project *by design*, so "list everything" is unreachable and stays that way. This is a
    new read that resolves its scope from **org membership first** and only then reads
    project by project — the fail-closed guard is untouched, and the unscoped path is
    never entered even internally.

    A **join, not a new write path**: every number here already exists in a table. A figure
    with no query behind it does not belong on the screen.

    The load-bearing case is the project that has never synced. It **appears**, with a
    `never` status — omitting it would shrink the org and hide precisely the projects that
    need attention. Its item counts are real, because the cloud is authoritative for items
    when a box is linked; only its node count is zero. `never` and `stale` are different
    words for the same reason they are on the deployments screen: a link set up and not
    finished is not a box that stopped.
    """
    readable = set(authz.readable_project_ids(db, user_id))
    projects = [
        p for p in db.scalars(
            select(Project).where(Project.org_id == org_id).order_by(Project.name)
        )
        if p.id in readable
    ]

    rows: list[dict] = []
    for p in projects:
        counts = {s: 0 for s in items_svc.STATUSES}
        for status, n in db.execute(
            select(Item.status, func.count()).where(Item.project_id == p.id).group_by(Item.status)
        ):
            if status in counts:
                counts[status] = n

        claims = [
            {"item_id": i.id, "title": i.title, "agent": i.claimed_by,
             "claimed_at": i.claimed_at.isoformat() if i.claimed_at else None}
            for i in db.scalars(
                select(Item).where(Item.project_id == p.id, Item.claimed_by.is_not(None))
                .order_by(Item.claimed_at)
            )
        ]

        nodes = db.scalar(
            select(func.count()).select_from(CodeNode).where(
                CodeNode.project_id == p.id, CodeNode.fresh.is_(True)
            )
        ) or 0
        last_push = db.scalar(
            select(func.max(Event.ts)).where(
                Event.action == "sync_code_graph", Event.project_id == p.id
            )
        )

        rows.append({
            "id": p.id, "tag": p.tag, "name": p.name, "accent": p.accent,
            "items": counts,
            "open_items": sum(counts[s] for s in items_svc.STATUSES if s != "done"),
            "claims": claims,
            "nodes": nodes,
            "last_push_at": last_push.isoformat() if last_push else None,
            "sync": "live" if last_push else "never",
        })

    org = db.get(Organization, org_id)
    return {
        "org_id": org_id,
        "plan": org.plan if org else None,
        "projects": rows,
        "totals": {
            "projects": len(rows),
            "open_items": sum(r["open_items"] for r in rows),
            "claims": sum(len(r["claims"]) for r in rows),
            "nodes": sum(r["nodes"] for r in rows),
            # Counted, not inferred from the rows above: a project the caller cannot read
            # is still a project that has never synced, and the nudge on an empty org
            # depends on this being the truth about the ORG rather than about the viewer.
            "never_synced": sum(1 for r in rows if r["sync"] == "never"),
        },
        "usage": quotas.usage(db, org_id),
        "limits": quotas.plan_of(org).__dict__ if org else {},
    }
