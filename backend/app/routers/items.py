from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas import ItemCreate, ItemOut, ItemUpdate, ReorderIn
from app.security import authz
from app.security.deps import get_current_user
from app.services import collision as collision_svc
from app.services import items as items_svc

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
    try:
        # Completion fires the judge and the lesson extractor, which are model calls
        # (GRPH-399). Scheduled here so the response does not wait on them.
        item = items_svc.update_item(db, item_id, defer=background.add_task,
                                     **body.model_dump(exclude_unset=True))
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
