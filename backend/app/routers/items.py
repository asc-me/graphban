from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas import (
    ItemCreate, ItemOut, ItemUpdate, OwnerCloseIn, ReorderIn, SnoozeIn, WaitAnswerIn,
)
from app.security import authz
from app.security.deps import get_current_user
from app.services import collision as collision_svc
from app.services import items as items_svc
from app.services import waits as waits_svc

router = APIRouter(prefix="/items", tags=["items"])


@router.get("", response_model=list[ItemOut])
def list_items(
    project_id: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_readable(db, user.id, project_id)
    return items_svc.list_items(db, project_id=project_id, status=status)


@router.post("", response_model=ItemOut, status_code=201)
def create_item(
    body: ItemCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    writable = authz.writable_project_ids(db, user.id)
    pid = body.project_id or (writable[0] if writable else None)
    authz.require_writable(db, user.id, pid)
    reporter = {"name": user.name, "handle": user.handle, "avatar": user.avatar}
    try:
        return items_svc.create_item(
            db,
            title=body.title,
            description=body.description,
            tags=body.tags,
            effort=body.effort,
            status=body.status,
            project_id=pid,
            touchpoints=body.touchpoints,
            prd_id=body.prd_id,
            prd_section=body.prd_section,
            reporter=reporter,
            question=body.question,
            options=body.options,
        )
    except items_svc.MissingAttestation as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.patch("/reorder", response_model=list[ItemOut])
def reorder(
    body: ReorderIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Every project the reordered items span must be writable by the caller.
    rows = [items_svc.get_item(db, i) for i in body.ordered_ids]
    for pid in {r.project_id for r in rows if r is not None}:
        authz.require_writable(db, user.id, pid, "item")
    return items_svc.reorder_items(db, body.ordered_ids)


@router.get("/collision-clusters")
def collision_clusters(
    project_id: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Non-colliding clusters over a project's items (AL-192 / PRD-10 v2): items whose
    (actual-or-predicted) code touch-areas overlap are grouped, so distinct groups are safe
    to run concurrently. Defaults to the unstarted pool (backlog + next) when no status."""
    authz.require_readable(db, user.id, project_id)
    return {"clusters": collision_svc.clusters_for_project(db, project_id, status)}


@router.get("/blockers")
def blockers(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Blocked items for triage (snoozed ones hidden); decision waits carry question/options."""
    authz.require_readable(db, user.id, project_id)
    return {"blockers": waits_svc.triage_blockers(db, project_id)}


@router.get("/{item_id}", response_model=ItemOut)
def get_item(item_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = items_svc.get_item(db, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    # Authorize by the item's project — without this, any authenticated user could
    # read any item by id, across tenants (AL-76 caught this). 404 hides existence.
    authz.require_readable(db, user.id, item.project_id, "item")
    return item


@router.patch("/{item_id}", response_model=ItemOut)
def update_item(
    item_id: str,
    body: ItemUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = items_svc.get_item(db, item_id)
    if existing is None:
        raise HTTPException(404, "item not found")
    authz.require_writable(db, user.id, existing.project_id, "item")
    changes = body.model_dump(exclude_unset=True)
    # GRPH-832: `reach` is set HERE and never by `update_item`, because `update_item` is the
    # verb every agent holds and this route is the one a bearer JWT reaches. Popped before the
    # service call rather than filtered inside it, so the split is visible at the boundary
    # that enforces it.
    declared_reach = changes.pop("reach", None)
    if any(isinstance(e, dict) and e.get("adapter") == items_svc.OWNER_CLOSE_ADAPTER
           for e in (changes.get("evidence") or [])):
        # THE CALL (GRPH-1030). JWT PATCH can otherwise mint any attestation the gate
        # will read, including one wearing this adapter. Owner-close has its own
        # route so the closer, the adapter name, and the owner check cannot drift.
        raise HTTPException(
            403,
            "an owner.close receipt cannot be written via PATCH; "
            "POST /api/items/{id}/owner-close",
        )
    try:
        if declared_reach is not None:
            items_svc.set_reach(db, existing, declared_reach)
        # Completion fires the judge and the lesson extractor, which are model calls
        # (GRPH-399). Scheduled here so the response does not wait on them.
        item = items_svc.update_item(db, item_id, defer=background.add_task, **changes)
    except items_svc.MissingAttestation as e:
        # 409, not 422: the request is well formed and the caller is permitted — the ITEM is
        # not in a state that may be completed (GRPH-543). A 422 would read as "you sent
        # something malformed" and send the caller editing their payload.
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if item is None:
        raise HTTPException(404, "item not found")
    return item


@router.post("/{item_id}/owner-close", response_model=ItemOut)
def owner_close(
    item_id: str,
    body: OwnerCloseIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Close a hand-merged item as the project owner (GRPH-1030). JWT only."""
    existing = items_svc.get_item(db, item_id)
    if existing is None:
        raise HTTPException(404, "item not found")
    authz.require_project_owner(db, user.id, existing.project_id)
    closer = {"id": user.id, "handle": user.handle, "name": user.name}
    try:
        item = items_svc.owner_close(
            db, item_id, closer=closer, commit=body.commit, pr_url=body.pr_url,
            defer=background.add_task,
        )
    except items_svc.MissingAttestation as e:
        raise HTTPException(409, str(e))
    except items_svc.PRCooldown as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if item is None:
        raise HTTPException(404, "item not found")
    return item


def _blocker_action(db: Session, user: User, item_id: str, run):
    existing = items_svc.get_item(db, item_id)
    if existing is None:
        raise HTTPException(404, "item not found")
    authz.require_writable(db, user.id, existing.project_id, "item")
    try:
        return run(existing.id)
    except waits_svc.ActionRefused as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/{item_id}/answer")
def answer_wait(item_id: str, body: WaitAnswerIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Answer a decision wait: stores the answer, marks it done, releases what waited on it."""
    return _blocker_action(db, user, item_id, lambda i: waits_svc.answer_wait(
        db, i, choice=body.option, text=body.text, actor=user))


@router.post("/{item_id}/unblock")
def unblock(item_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _blocker_action(db, user, item_id, lambda i: waits_svc.unblock(db, i, actor=user))


@router.post("/{item_id}/release-claim")
def release_claim(item_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    return _blocker_action(db, user, item_id, lambda i: waits_svc.release_claim(db, i, actor=user))


@router.post("/{item_id}/escalate")
def escalate(item_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _blocker_action(db, user, item_id, lambda i: waits_svc.escalate(db, i, actor=user))


@router.post("/{item_id}/snooze")
def snooze(item_id: str, body: SnoozeIn, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    return _blocker_action(db, user, item_id, lambda i: waits_svc.snooze(
        db, i, until=body.until, actor=user))


@router.post("/{item_id}/undo")
def undo_blocker_action(item_id: str, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Undo the newest blocker action on the item; refused once anything else changed it."""
    return _blocker_action(db, user, item_id, lambda i: waits_svc.undo(db, i, actor=user))
