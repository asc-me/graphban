"""The learning loop's HTTP surface (GRPH-310 / GRPH-344 / PRD-16).

One router rather than two, because the web app and the MCP tools call the same service
functions — PRD-16 asks for the loop to be exposed "through the same service layer the web
app calls", and a second implementation is how the two drift until one of them is wrong.

The `/used` route is the odd one out and deserves its own note: it authenticates with an
API KEY rather than a session, because the caller is a generated hook running on somebody's
machine at 3am, not a person with a browser open.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, ArtifactRecommendation, User
from app.security import authz
from app.security.deps import get_agent_key, get_current_user
from app.services import artifact_inventory as inv_svc
from app.services import artifacts as art_svc

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


def _rec_dict(r: ArtifactRecommendation) -> dict:
    return {
        "id": r.id, "tier": r.tier, "scope": r.scope, "title": r.title,
        "reasoning": r.reasoning, "status": r.status, "target": r.target,
        "lesson_ids": r.lesson_ids or [], "supersedes_id": r.supersedes_id,
        "graded_by": r.graded_by, "draft_path": r.draft_path,
        "install_class": r.install_class, "has_draft": bool(r.draft),
    }


@router.get("/recommendations")
def list_recommendations(project_id: str | None = None, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """Recommendations awaiting a human. Superseded rows are excluded — they are history,
    not a queue."""
    authz.require_readable(db, user.id, project_id)
    return [_rec_dict(r) for r in art_svc.pending(db, project_id)]


@router.get("/recommendations/{rec_id}")
def get_recommendation(rec_id: int, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """One recommendation with its draft and its install plan.

    The plan travels WITH the artifact so a caller cannot render an install button for
    something that may never be written — the refusal and the contents arrive together."""
    rec = db.get(ArtifactRecommendation, rec_id)
    if rec is None:
        raise HTTPException(404, "no such recommendation")
    authz.require_readable(db, user.id, rec.project_id)
    out = _rec_dict(rec) | {"draft": rec.draft}
    try:
        out["install"] = art_svc.install_plan(db, rec)
    except art_svc.InstallRefused as e:
        out["install"] = {"allowed": False, "reason": str(e), "contents": "", "path": ""}
    return out


class ReviewIn(BaseModel):
    decision: str  # approve | reject


@router.post("/recommendations/{rec_id}/review")
def review_recommendation(rec_id: int, body: ReviewIn, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """Approve or reject. The human boundary — nothing installs without passing here.

    A decision is never re-asked: classification skips lessons that already carry a
    recommendation precisely so a later run cannot flip this back to `queued`."""
    rec = db.get(ArtifactRecommendation, rec_id)
    if rec is None:
        raise HTTPException(404, "no such recommendation")
    authz.require_writable(db, user.id, rec.project_id)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve or reject")
    rec.status = "approved" if body.decision == "approve" else "rejected"
    db.commit()
    db.refresh(rec)
    return _rec_dict(rec)


@router.get("/usage")
def usage(project_id: str | None = None, db: Session = Depends(get_db),
          user: User = Depends(get_current_user)):
    """Population and usage, with the unmeasurable half NAMED rather than hidden — `uses`
    is null for a tier whose use cannot be observed, never 0."""
    authz.require_readable(db, user.id, project_id)
    return art_svc.usage_report(db, project_id)


@router.get("/stale")
def stale(project_id: str | None = None, db: Session = Depends(get_db),
          user: User = Depends(get_current_user)):
    """Measurable artifacts with no observed use in the window. Never includes a tier whose
    usage cannot be observed, because zero uses there is not evidence of disuse."""
    authz.require_readable(db, user.id, project_id)
    return [_rec_dict(r) for r in art_svc.stale_artifacts(db, project_id)]


class InventoryItemIn(BaseModel):
    path: str
    tier: str = ""
    content_hash: str = ""
    size: int = 0


class InventoryIn(BaseModel):
    root: str
    items: list[InventoryItemIn]
    project_id: str | None = None


@router.post("/inventory")
def post_inventory(body: InventoryIn, db: Session = Depends(get_db),
                   key: ApiKey = Depends(get_agent_key)):
    """A client reporting what is actually installed on its machine (GRPH-354).

    **API key, and a client-side scan, because the server cannot see the files.** Under
    `hosted_mode` it has no access to anyone's `.claude/` directory, and inside the
    docker-compose container it has no access to the host's either — a server-side walk would
    report a population of zero without erroring, which is exactly the failure this endpoint
    exists to close.

    Read-only in both directions: the server records what it is told and never writes,
    moves, or deletes anything on the reporting machine.

    Orphaning is scoped to the `root` given. A scan of `~/.claude` says nothing about what
    lives under `~/work/.cursor`, and marking those missing because this pass did not look
    there would be an absence read as a finding.
    """
    project_id = body.project_id or key.project_id or "core"
    authz.require_writable(db, key.user_id, project_id)
    if not body.root:
        raise HTTPException(422, "root is required — it scopes which rows may be orphaned")
    return inv_svc.record_scan(db, project_id=project_id, root=body.root,
                               items=[i.model_dump() for i in body.items])


@router.get("/inventory")
def get_inventory(project_id: str | None = None, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """What is installed, generated and hand-written alike, with `forked` and `orphaned`
    named rather than inferred."""
    authz.require_readable(db, user.id, project_id)
    return [{"id": r.id, "path": r.path, "tier": r.tier, "root": r.root,
             "state": r.state, "recommendation_id": r.recommendation_id,
             "size": r.size, "last_seen": r.last_seen}
            for r in inv_svc.inventory(db, project_id)]


@router.post("/{rec_id}/used")
def record_use(rec_id: int, db: Session = Depends(get_db),
               key: ApiKey = Depends(get_agent_key)):
    """A generated hook reporting that it fired (GRPH-344).

    **This endpoint is what makes hook instrumentation real.** GRPH-309 appends a telemetry
    line to every generated hook, and that line swallows its own failures so a telemetry
    outage can never break the workflow — which means a missing route here would be
    completely silent. Every hook would report nothing, forever, and zero observed uses on
    a measurable tier queues a DELETE. The instrumentation would have retired exactly the
    hooks it was built to protect.

    Authenticated by API KEY rather than a session: the caller is a script on somebody's
    machine, not a person with a browser open.
    """
    rec = db.get(ArtifactRecommendation, rec_id)
    if rec is None:
        raise HTTPException(404, "no such artifact")
    authz.require_writable(db, key.user_id, rec.project_id)
    art_svc.record_use(db, rec_id, signal="hook-self-report")
    return {"ok": True, "recommendation_id": rec_id}
