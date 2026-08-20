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

from app.models import CodeNode, Project, ProjectEdge, utcnow

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
