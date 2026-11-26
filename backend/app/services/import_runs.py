"""The import run and its report (GRPH-1951 / P92 D3).

A run is a record: project, commit, sources read and unread, and each finding with its bucket.
Findings in the `proposed` bucket file a proposal through `rule_proposals.propose` with the run
as proposer (`import:<id>`), so every pipeline guarantee (cited, direction-checked, never off,
no invariants) applies unchanged. `gap` findings file guidance; `external` findings are
recorded only. A finding the pipeline refuses (uncited, loosening an invariant, re-filing a
dismissed proposal) is kept on the run with the refusal as `refused`, so one bad finding neither
aborts the import nor leaves a half-written run behind. Adoption goes through `rule_proposals.adopt`, the pipeline's batch adoption.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, NotFound, Refused, Validation
from app.models import ImportFinding, ImportRun, Project, RuleProposal, User
from app.security import authz
from app.services import handles, rule_pack, rule_proposals

BUCKETS = ("proposed", "external", "gap")

# ---- prose sources (GRPH-1952 / P92 D4) ------------------------------------------------------
# Prose is read by the customer's own agent through the `graphban-rules-import` skill, never by
# the CLI and never by a model on the box. So a prose source is UNREAD until an agent attached
# to the run says it read it, and the run is not clean while one is. The CLI cannot claim one
# read: `create_run` moves any prose path it lists as read back to unread.
PROSE_FILES = ("CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", ".cursorrules",
               ".github/copilot-instructions.md")
PROSE_PREFIXES = ("docs/adr/", "doc/adr/", "adr/", "docs/architecture")
AGENT_READER = "agent"
AWAITING_AGENT = ("prose: no agent attached to this run has read it "
                  "(run the graphban-rules-import skill)")


def is_prose(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return path in PROSE_FILES or base in ("CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md") or (
        path.startswith(PROSE_PREFIXES) and path.lower().endswith((".md", ".markdown", ".txt",
                                                                    ".rst", ".adoc")))


def _awaiting(s: dict) -> dict:
    return {**s, "reader": AGENT_READER, "reason": s.get("reason") or AWAITING_AGENT}


def _str(v: object, what: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise Validation(f"`{what}` must be a non-empty string")
    return v.strip()


def _sources(raw: object, what: str) -> list[dict]:
    out = []
    for s in raw or []:
        if isinstance(s, str):
            s = {"path": s}
        if not isinstance(s, dict) or not isinstance(s.get("path"), str) or not s["path"]:
            raise Validation(f"each of `{what}` needs a `path`")
        out.append(dict(s))
    return out


def create_run(db: Session, user: User, project_id: str, *, commit: object,
               sources_read: object = None, sources_unread: object = None,
               findings: object = None) -> ImportRun:
    """Record a run and file every finding. Each finding is validated before the run is
    written; a proposal a finding files is committed by `propose`."""
    authz.require_writable(db, user.id, project_id)
    if db.get(Project, project_id) is None:
        raise NotFound(f"project {project_id!r} not found")
    sha = _str(commit, "commit")
    specs = findings if isinstance(findings, list) else []
    if findings is not None and not isinstance(findings, list):
        raise Validation("`findings` must be a list")
    for f in specs:
        if not isinstance(f, dict) or f.get("bucket") not in BUCKETS:
            raise Validation(f"each finding needs a `bucket` of {BUCKETS}")
        if f.get("stage") not in rule_pack.STAGES:
            raise Validation(f"each finding's `stage` must be one of {rule_pack.STAGES}")
        _str(f.get("quote"), "quote")
        if not isinstance(f.get("source"), dict) or not f["source"].get("path"):
            raise Validation("each finding needs a `source` with a `path`")
    read = _sources(sources_read, "sources_read")
    unread = [_awaiting(s) if is_prose(s["path"]) else s
              for s in _sources(sources_unread, "sources_unread")]
    # Only an agent attached to the run marks prose read (`attach`); a run opened by a person
    # or the CLI lists every prose source as unread, so its report never reads as clean.
    unread += [_awaiting({k: v for k, v in s.items() if k != "reason"})
               for s in read if is_prose(s["path"])]
    read = [s for s in read if not is_prose(s["path"])]
    run = ImportRun(project_id=project_id, commit_sha=sha, sources_read=read,
                    sources_unread=unread,
                    created_by=handles.project_handle(db, user, project_id) or user.name)
    db.add(run)
    db.flush()
    who = f"import:{run.id}"
    db.commit()
    for f in specs:
        row = ImportFinding(run_id=run.id, bucket=f["bucket"], stage=f["stage"],
                            quote=f["quote"].strip(), source=dict(f["source"]))
        try:
            if f["bucket"] == "proposed":
                prop = dict(f.get("proposal") or {})
                p, _ = rule_proposals.propose(db, project_id=project_id, proposer_kind="import",
                                              proposer=who, **prop)
                row.proposal_id = p.id
            elif f["bucket"] == "gap":
                g = rule_proposals.file_guidance(
                    db, project_id=project_id, proposer_kind="import", proposer=who,
                    stage=f["stage"], label=f.get("label"), text=f.get("text"),
                    citations=f.get("citations"))
                row.label, row.text, row.guidance_id = g.label, g.text, g.id
            else:
                row.label = f.get("label")
                row.text = f.get("text")
        except (AppError, Refused, TypeError) as e:
            db.rollback()
            detail = e.detail if isinstance(e, Refused) else str(e)
            row.refused = str(detail)[:500]
            row.label = f.get("label") if isinstance(f.get("label"), str) else None
            row.text = f.get("text") if isinstance(f.get("text"), str) else None
        db.add(row)
        db.commit()
    return run


def _get(db: Session, project_id: str, run_id: int) -> ImportRun:
    run = db.get(ImportRun, run_id)
    if run is None or run.project_id != project_id:
        raise NotFound(f"import run {run_id!r} not found in this project")
    return run


def report(db: Session, project_id: str, run_id: int) -> dict:
    """The run with its findings, ordered stage then bucket. Unread sources lead."""
    run = _get(db, project_id, run_id)
    rows = list(db.scalars(select(ImportFinding).where(ImportFinding.run_id == run.id)
                           .order_by(ImportFinding.id)))
    stages = {s: i for i, s in enumerate(rule_pack.STAGES)}
    rows.sort(key=lambda r: (stages.get(r.stage, 99), BUCKETS.index(r.bucket), r.id))
    props = {p.id: p for p in db.scalars(select(RuleProposal).where(
        RuleProposal.id.in_([r.proposal_id for r in rows if r.proposal_id])))} if rows else {}
    return {
        "id": run.id, "project_id": run.project_id, "commit": run.commit_sha,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "sources_unread": run.sources_unread or [], "sources_read": run.sources_read or [],
        # Clean only when nothing went unread and the pipeline refused nothing.
        "clean": not (run.sources_unread or []) and not any(r.refused for r in rows),
        "findings": [{
            "id": r.id, "bucket": r.bucket, "stage": r.stage, "quote": r.quote,
            "source": r.source, "label": r.label, "text": r.text,
            "proposal_id": r.proposal_id, "guidance_id": r.guidance_id, "refused": r.refused,
            "proposal": rule_proposals.view(props[r.proposal_id]) if r.proposal_id in props
            else None,
        } for r in rows],
    }


def list_runs(db: Session, project_id: str) -> list[dict]:
    rows = db.scalars(select(ImportRun).where(ImportRun.project_id == project_id)
                      .order_by(ImportRun.id.desc()))
    return [{"id": r.id, "commit": r.commit_sha, "created_by": r.created_by,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "unread": len(r.sources_unread or []),
             "unread_prose": sum(1 for s in (r.sources_unread or [])
                                 if s.get("reader") == AGENT_READER)} for r in rows]


def adopt_batch(db: Session, actor: object, project_id: str, run_id: int,
                finding_ids: list[int], reason: str) -> list[RuleProposal]:
    """Adopt the ticked findings' proposals as one batch under one reason (pipeline D4)."""
    run = _get(db, project_id, run_id)
    ids = list(dict.fromkeys(finding_ids))
    if not ids:
        raise Validation("tick at least one finding to adopt")
    found = {f.id: f for f in db.scalars(select(ImportFinding).where(
        ImportFinding.run_id == run.id, ImportFinding.id.in_(ids)))}
    pids = []
    for i in ids:
        f = found.get(i)
        if f is None:
            raise NotFound(f"finding {i} is not part of import run {run.id}")
        if f.proposal_id is None:
            why = f" (refused: {f.refused})" if f.refused else ""
            raise Validation(f"finding {i} is a `{f.bucket}` finding with no proposal to adopt"
                             + why)
        pids.append(f.proposal_id)
    return rule_proposals.adopt(db, actor, project_id, pids, reason)


# ---- the skill's half (GRPH-1952 / P92 D4) ---------------------------------------------------

def _mark_read(run: ImportRun, paths: object, who: str) -> None:
    if paths is None:
        return
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise Validation("`sources_read` must be a list of paths")
    unread = list(run.sources_unread or [])
    for p in paths:
        hit = next((s for s in unread if s.get("path") == p
                    and s.get("reader") == AGENT_READER), None)
        if hit is None:
            # A parser's unread source (a token without permission, a cross-repo path) is not
            # the agent's to clear, and neither is a path the run never listed.
            raise Validation(f"{p!r} is not a prose source awaiting an agent on import run "
                             f"{run.id}", hint="name a path from the run's `sources_unread` "
                             "whose reader is `agent`")
        unread.remove(hit)
    read = list(run.sources_read or [])
    read += [{"path": p, "reader": AGENT_READER, "read_by": who} for p in paths]
    run.sources_unread, run.sources_read = unread, read


def _file_source(run: ImportRun, citations: object) -> dict:
    """The finding's source: its first `file` citation, which must cite the run's commit."""
    files = [c.get("ref") for c in citations if isinstance(c, dict) and c.get("kind") == "file"
             and isinstance(c.get("ref"), dict)] if isinstance(citations, list) else []
    if not files:
        raise Refused(422, "a finding on an import run cites the line it relies on: a `file` "
                      "citation {path, lines, commit}", rule_id=rule_proposals.CITED_RULE)
    for f in files:
        if f.get("commit") != run.commit_sha:
            raise Refused(422, f"{f.get('path')!r} is cited at {str(f.get('commit'))[:12]}, "
                          f"but import run {run.id} read {run.commit_sha[:12]}",
                          rule_id=rule_proposals.CITED_RULE,
                          hint="cite the line at the run's commit")
    f = files[0]
    return {"path": f.get("path"), "lines": str(f.get("lines") or ""), "commit": run.commit_sha}


def attach(db: Session, project_id: str, run_id: object, *, proposer: str, kind: str | None,
           draft: dict, citations: object, quote: object = None,
           sources_read: object = None) -> dict:
    """An agent's finding on a run the CLI opened, filed through the pipeline as the agent.

    The proposer stays `agent`, so everything an agent may not do (loosen, propose an
    invariant, file uncited) is refused exactly as without a run. The run gains a finding
    either way: a refused one carries the refusal, and the refusal is raised to the agent.
    `sources_read` clears the run's prose sources the agent read; with no `kind` that is all
    the call does.
    """
    if isinstance(run_id, bool) or not str(run_id).strip().isdigit():
        raise Validation("`import_run` is the run's numeric id, as `gban rules import --prose` "
                         "printed it")
    run = _get(db, project_id, int(run_id))
    if not isinstance(draft, dict):
        raise Validation("`draft` must be an object")
    _mark_read(run, sources_read, proposer)
    db.commit()
    if not kind:
        out = report(db, project_id, run.id)
        return {"import_run": run.id, "sources_read": out["sources_read"],
                "sources_unread": out["sources_unread"], "clean": out["clean"]}
    q = _str(quote, "quote")
    stage = draft.get("stage")
    row = ImportFinding(run_id=run.id, quote=q, source={}, stage="",
                        bucket="external" if kind == "external" else (
                            "gap" if kind == rule_proposals.GUIDANCE_KIND else "proposed"))
    filed: dict = {}
    err: Exception | None = None
    try:
        row.source = _file_source(run, citations)
        if kind == "external":
            # Enforced outside Graphban: recorded on the run, nothing filed. Still cited, and
            # the citation is verified like any other.
            rule_proposals.verify_citations(db, project_id, citations)
            if stage not in rule_pack.STAGES:
                raise Validation(f"`draft.stage` must be one of {rule_pack.STAGES}")
            row.stage, row.label, row.text = stage, draft.get("label"), draft.get("text")
        elif kind == rule_proposals.GUIDANCE_KIND:
            g = rule_proposals.file_guidance(
                db, project_id=project_id, proposer_kind="agent", proposer=proposer,
                stage=stage, label=draft.get("label"), text=draft.get("text"),
                citations=citations)
            row.stage, row.label, row.text, row.guidance_id = g.stage, g.label, g.text, g.id
            filed = rule_proposals.guidance_view(g)
        else:
            p, folded = rule_proposals.propose(
                db, project_id=project_id, proposer_kind="agent", proposer=proposer,
                kind=kind, citations=citations, rule_id=draft.get("rule_id"),
                slug=draft.get("slug"), primitive=draft.get("primitive"),
                title=draft.get("title"), why=draft.get("why"), params=draft.get("params"))
            prim = rule_pack.PRIMITIVES.get(p.primitive or "")
            row.stage = (p.draft or {}).get("stage") or (prim.stage if prim else "")
            row.proposal_id, row.label = p.id, draft.get("title") or p.rule_id
            filed = rule_proposals.view(p, folded=folded)
    except (AppError, Refused, TypeError) as e:
        db.rollback()
        err = e
        detail = e.detail if isinstance(e, Refused) else str(e)
        row.refused = str(detail)[:500]
        row.stage = stage if stage in rule_pack.STAGES else row.stage or ""
        row.label = draft.get("label") or draft.get("title")
        row.text = draft.get("text") if isinstance(draft.get("text"), str) else None
    db.add(row)
    db.commit()
    if err is not None:
        raise err
    return {**filed, "import_run": run.id, "finding_id": row.id, "bucket": row.bucket,
            "sources_unread": list(run.sources_unread or [])}
