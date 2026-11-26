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
    run = ImportRun(project_id=project_id, commit_sha=sha,
                    sources_read=_sources(sources_read, "sources_read"),
                    sources_unread=_sources(sources_unread, "sources_unread"),
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
             "unread": len(r.sources_unread or [])} for r in rows]


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
