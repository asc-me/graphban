from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import MemoryShard, User
from app.schemas import (
    CandidateJudgeOut,
    LessonDetail,
    LessonListOut,
    LessonOutcomeIn,
    MemorySearchIn,
    PromoteOrgIn,
    ScoredCandidate,
    ShardCreate,
    ShardHit,
    ShardOut,
)
from app.security import authz
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import memory as mem_svc
from app.services import quotas

router = APIRouter(prefix="/memory", tags=["memory"])


class ShardEdit(BaseModel):
    text: str


class ImportIn(BaseModel):
    shards: list[dict]
    project_id: str = "core"


@router.get("/lessons", response_model=LessonListOut)
def list_lessons(
    project_id: str,
    trend: str | None = None,
    caught_state: str | None = None,
    eligibility: str | None = None,
    lesson_class: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Published lesson catalog. Empty results are 'no published lessons', not a score."""
    authz.require_readable(db, user.id, project_id)
    return mem_svc.list_lessons(
        db,
        project_id,
        readable_project_ids=set(authz.readable_project_ids(db, user.id)),
        filters={
            "trend": trend,
            "caught_state": caught_state,
            "eligibility": eligibility,
            "lesson_class": lesson_class,
        },
        limit=limit,
        offset=offset,
    )


@router.get("/lessons/{shard_id}", response_model=LessonDetail)
def get_lesson(
    shard_id: str,
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Published lesson detail. Project-local of another project 404s; org-reach sibling 200s."""
    authz.require_readable(db, user.id, project_id)
    row = mem_svc.get_lesson(
        db, shard_id,
        readable_project_ids=set(authz.readable_project_ids(db, user.id)),
        viewer_project_id=project_id,
    )
    if row is None:
        raise HTTPException(404, "lesson not found")
    return row


@router.post("/lessons/{shard_id}/outcomes", response_model=LessonDetail)
def record_lesson_outcome(
    shard_id: str,
    body: LessonOutcomeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Human-recorded caught / missed / contradicted. JWT only — copy publish_shard."""
    existing = db.get(MemoryShard, shard_id)
    if existing is None or existing.status != "published":
        raise HTTPException(404, "lesson not found")
    if existing.project_id is not None:
        authz.require_writable(db, user.id, existing.project_id, "shard")
    elif not authz.writable_project_ids(db, user.id):
        raise HTTPException(403, "no write access to any project")
    if body.kind not in ("caught", "missed", "contradicted"):
        raise HTTPException(422, f"invalid outcome kind: {body.kind}")
    mem_svc.record_outcome(
        db, shard_id, kind=body.kind, source="human", detail=body.detail,
    )
    events_svc.record_user(
        db, user, action="record_lesson_outcome", target_type="shard",
        target_id=shard_id, project_id=existing.project_id,
        meta={"kind": body.kind},
    )
    readable = set(authz.readable_project_ids(db, user.id))
    row = mem_svc.get_lesson(
        db, shard_id,
        readable_project_ids=readable,
        viewer_project_id=mem_svc.lesson_reload_viewer(db, existing, readable),
        skip_visibility=True,
    )
    if row is None:
        raise HTTPException(404, "lesson not found")
    return row


@router.post("/lessons/{shard_id}/promote-org", response_model=LessonDetail)
def promote_org_lesson(
    shard_id: str,
    body: PromoteOrgIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Human-only org promotion. X-API-Key → 401. Unverifiable cannot be overridden."""
    existing = db.get(MemoryShard, shard_id)
    if existing is None or existing.status != "published":
        raise HTTPException(404, "lesson not found")
    if existing.project_id is not None:
        authz.require_writable(db, user.id, existing.project_id, "shard")
    elif not authz.writable_project_ids(db, user.id):
        raise HTTPException(403, "no write access to any project")
    body = body or PromoteOrgIn()
    try:
        result = mem_svc.promote_org(
            db, shard_id,
            override_reason=body.override_reason,
            readable_project_ids=set(authz.readable_project_ids(db, user.id)),
        )
    except mem_svc.PromoteRefused as e:
        raise HTTPException(422, e.payload) from e
    lesson = result.get("lesson")
    if lesson is None:
        raise HTTPException(500, "lesson could not be reloaded after promote")
    if result["wrote"]:
        events_svc.record_user(
            db, user, action="promote_org_lesson", target_type="shard",
            target_id=shard_id, project_id=existing.project_id,
            meta={
                "eligibility": result["eligibility"],
                "override_reason": (
                    (body.override_reason or "").strip() or None
                    if result["overridden"] else None
                ),
                "transferability": lesson["transferability"],
            },
        )
    return lesson


@router.get("/shards", response_model=list[ShardOut])
def list_shards(
    project_id: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """`limit` caps live shards returned. The sidebar browses the newest few and searches for
    the rest; it was pulling the entire corpus to render a scrolling list (GRPH-431)."""
    authz.require_readable(db, user.id, project_id)
    return mem_svc.list_shards(db, project_id=project_id, status=status, limit=limit)


@router.get("/candidates", response_model=list[ShardOut])
def list_candidates(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The review queue (AL-49): agent-written shards awaiting human publish."""
    authz.require_readable(db, user.id, project_id)
    return mem_svc.list_shards(db, project_id=project_id, status="candidate")


@router.get("/candidates/scored", response_model=list[ScoredCandidate])
def scored_candidates(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The review queue with advisory accept/reject suggestions (AL-151): each candidate
    scored by similarity to trusted/rejected memory + recurrence, most actionable first.
    Advisory only — publishing/rejecting is still the human's call."""
    authz.require_readable(db, user.id, project_id)
    return [
        ScoredCandidate(
            shard=ShardOut.model_validate(r["shard"]),
            suggestion=r["suggestion"],
            confidence=r["confidence"],
            reasons=r["reasons"],
            duplicate_of=r["duplicate_of"],
        )
        for r in mem_svc.score_candidates(db, project_id=project_id)
    ]


class ShardCluster(BaseModel):
    size: int
    representative: ShardOut
    members: list[ShardOut]  # the duplicates (representative excluded)


@router.get("/candidate-clusters", response_model=list[ShardCluster])
def candidate_clusters(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Recurring lessons (AL-50): candidate shards grouped by similarity, so a
    correction that keeps recurring can be promoted once as a principle."""
    authz.require_readable(db, user.id, project_id)
    groups = mem_svc.cluster_candidates(db, project_id=project_id)
    return [
        ShardCluster(
            size=len(g),
            representative=ShardOut.model_validate(g[0]),
            members=[ShardOut.model_validate(s) for s in g[1:]],
        )
        for g in groups
    ]


class PromoteClusterIn(BaseModel):
    publish_id: str
    reject_ids: list[str] = []


@router.post("/promote-cluster")
def promote_cluster(body: PromoteClusterIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Give a recurring lesson one durable owner: publish the representative and
    reject the duplicates (AL-50). Each shard is authz-checked and audited."""
    _review_shard(body.publish_id, "published", "publish_shard", db, user)
    for rid in body.reject_ids:
        _review_shard(rid, "rejected", "reject_shard", db, user)
    return {"published": body.publish_id, "rejected": body.reject_ids}


@router.post("/shards", response_model=ShardOut, status_code=201)
def add_shard(body: ShardCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if body.project_id is not None:
        authz.require_writable(db, user.id, body.project_id)
        quotas.enforce_shard_quota(db, quotas.org_id_for_project(db, body.project_id))
    elif settings.hosted_mode:
        # Hosted tenants can't create project-less "global" memory — it would be
        # visible tenant-wide and break isolation (AL-71). Reads already exclude it.
        raise HTTPException(400, "global memory is disabled; specify a project_id")
    elif not authz.writable_project_ids(db, user.id):
        # A global (project-less) shard still requires write access somewhere.
        raise HTTPException(403, "no write access to any project")
    # A human wrote it → published straight away (the trust boundary is for agents).
    return mem_svc.add_memory(
        db,
        text_body=body.text,
        scope=body.scope,
        item_id=body.item_id,
        project_id=body.project_id,
        status="published",
        origin=f"user:{user.handle or user.id}",
        actor_user_id=user.id,
        attributed_project_id=body.project_id,
    )


def _review_shard(shard_id: str, to_status: str, action: str, db: Session, user: User) -> MemoryShard:
    existing = db.get(MemoryShard, shard_id)
    if existing is None:
        raise HTTPException(404, "shard not found")
    if existing.project_id is not None:
        authz.require_writable(db, user.id, existing.project_id, "shard")
    elif not authz.writable_project_ids(db, user.id):
        raise HTTPException(403, "no write access to any project")
    if to_status == "published":
        mem_svc.stamp_publish_attribution(existing, actor_user_id=user.id)
    shard = mem_svc.set_status(db, shard_id, to_status)
    events_svc.record_user(db, user, action=action, target_type="shard",
                           target_id=shard_id, project_id=existing.project_id)
    return shard


@router.post("/shards/{shard_id}/publish", response_model=ShardOut)
def publish_shard(shard_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Promote a candidate into the trusted retrieval path (AL-49)."""
    return _review_shard(shard_id, "published", "publish_shard", db, user)


@router.post("/shards/{shard_id}/reject", response_model=ShardOut)
def reject_shard(shard_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Reject a candidate — kept for provenance, never surfaces in search (AL-49)."""
    return _review_shard(shard_id, "rejected", "reject_shard", db, user)


@router.post("/shards/{shard_id}/judge", response_model=CandidateJudgeOut)
def judge_shard(shard_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """On-demand LLM judge for a candidate (GRPH-650). Advisory — never mutates
    status. A missing verdict is a cause, not quality 0."""
    existing = db.get(MemoryShard, shard_id)
    if existing is None:
        raise HTTPException(404, "shard not found")
    if existing.project_id is not None:
        authz.require_writable(db, user.id, existing.project_id, "shard")
    elif not authz.writable_project_ids(db, user.id):
        raise HTTPException(403, "no write access to any project")
    return mem_svc.advisory_judge_view(db, existing)


@router.get("/auto-actions", response_model=list[ShardOut])
def auto_actions(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The "recent auto-actions" lane (AL-227): shards the scorer published or rejected
    without a human, newest first — so nothing auto-triaged is silent, and each can be undone."""
    authz.require_readable(db, user.id, project_id)
    return mem_svc.auto_triaged_shards(db, project_id=project_id)


@router.post("/shards/{shard_id}/undo-auto", response_model=ShardOut)
def undo_auto(shard_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Undo an auto-action (AL-227): return the shard to the candidate review queue and
    clear its auto-triage markers. Authz-checked and audited like a manual review."""
    existing = db.get(MemoryShard, shard_id)
    if existing is None:
        raise HTTPException(404, "shard not found")
    if existing.project_id is not None:
        authz.require_writable(db, user.id, existing.project_id, "shard")
    elif not authz.writable_project_ids(db, user.id):
        raise HTTPException(403, "no write access to any project")
    prior = existing.status
    shard = mem_svc.undo_triage(db, shard_id)
    events_svc.record_user(db, user, action="undo_auto_shard", target_type="shard",
                           target_id=shard_id, project_id=existing.project_id,
                           meta={"undid": prior})
    return shard


@router.patch("/shards/{shard_id}", response_model=ShardOut)
def edit_shard(shard_id: str, body: ShardEdit, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    existing = db.get(MemoryShard, shard_id)
    if existing is None:
        raise HTTPException(404, "shard not found")
    if existing.project_id is not None:
        authz.require_writable(db, user.id, existing.project_id, "shard")
    elif not authz.writable_project_ids(db, user.id):
        raise HTTPException(403, "no write access to any project")
    shard = mem_svc.update_shard(db, shard_id, text_body=body.text)
    if shard is None:
        raise HTTPException(404, "shard not found")
    return shard


@router.post("/search", response_model=list[ShardHit])
def search(body: MemorySearchIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, body.project_id)
    hits = mem_svc.search_memory(db, body.query, top_k=body.top_k, project_id=body.project_id)
    return [ShardHit(shard=ShardOut.model_validate(s), score=round(score, 4)) for s, score in hits]


@router.post("/backfill")
def backfill(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Re-embed all memory shards AND code nodes with the current provider — run
    after switching embedding providers or changing EMBED_DIM (AL-64).

    This is a global, cross-tenant maintenance op, so in hosted mode it's restricted
    to a platform operator — a single tenant must not be able to trigger a re-embed of
    every tenant's data (AL-76). Self-host is unrestricted, as before."""
    if settings.hosted_mode and not quotas.is_platform_admin(user):
        raise HTTPException(403, "backfill is an operator-only maintenance action")
    from app.services import code_graph as code_svc

    return {
        "reembedded": mem_svc.backfill_embeddings(db),
        "code_reembedded": code_svc.backfill_embeddings(db),
    }


@router.get("/export")
def export(project_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if project_id is None:
        raise HTTPException(422, "project_id is required")
    authz.require_readable(db, user.id, project_id)
    return {"shards": mem_svc.export_shards(db, project_id=project_id)}


@router.post("/import")
def import_(body: ImportIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, body.project_id)
    return {"imported": mem_svc.import_shards(db, body.shards, project_id=body.project_id)}
