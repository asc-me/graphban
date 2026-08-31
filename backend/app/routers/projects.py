import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tagging
from app.config import settings
from app.db import get_db
from app.models import Membership, Project, User
from app.schemas import (
    GitopsPatch, GitopsView, MemberOut, ProjectCreate, ProjectOut, ProjectRetagIn,
    ProjectUpdate, UserOut,
)
from app.security import authz
from app.security.deps import get_current_user
from app.services import code_sync
from app.services import events as events_svc
from app.services import gitops
from app.services import projects as projects_svc
from app.services import quotas

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("/tag-suggestion")
def tag_suggestion(name: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """A free tag derived from a project name, for prefilling the creation form.

    Derivation lives server-side on purpose: duplicating it in TypeScript would give the
    UI and the API two implementations to drift apart, and the one that matters is the
    one that actually assigns the tag.
    """
    return {"tag": projects_svc.unique_tag(db, name)}


@router.get("/tag-check")
def tag_check(tag: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Validity + availability for a tag the user typed, for live form feedback."""
    available, reason = projects_svc.tag_available(db, tag)
    return {"tag": tagging.normalize(tag), "available": available, "reason": reason}


def _resolve_org_id(db: Session, user: User, requested: str | None) -> str | None:
    """Pick the org a new project belongs to (hosted mode only, AL-74b).

    Self-host: always None — projects have no org. Hosted: the project MUST land in an
    org the creator belongs to, otherwise the AL-74 authz gate would make it instantly
    unreachable (org_id NULL ∉ the caller's orgs) and lock the creator out. A single-org
    user needs no choice; anyone in multiple orgs must name one."""
    if not settings.hosted_mode:
        return None
    orgs = authz.org_ids_for_user(db, user.id)
    if requested is not None:
        authz.require_org_member(db, user.id, requested)  # 404 if not a member
        return requested
    if len(orgs) == 1:
        return orgs[0]
    if not orgs:
        raise HTTPException(403, "create or join an organization before creating a project")
    raise HTTPException(422, "org_id is required: you belong to more than one organization")


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    readable = set(authz.readable_project_ids(db, user.id))
    rows = db.scalars(select(Project).order_by(Project.name)).all()
    return [p for p in rows if p.id in readable]


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    org_id = _resolve_org_id(db, user, body.org_id)
    quotas.enforce_project_quota(db, org_id)  # hosted plan cap (no-op self-host)
    try:
        project = projects_svc.create_project(
            db, name=body.name, owner_user_id=user.id, tag=body.tag,
            accent=body.accent, description=body.description, org_id=org_id,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    events_svc.record_user(db, user, action="create_project", target_type="project",
                           target_id=project.id, project_id=project.id, meta={"name": project.name})
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str,
    body: ProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_writable(db, user.id, project_id)
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    changed = body.model_dump(exclude_unset=True)
    for k, v in changed.items():
        if v is not None:
            setattr(project, k, v)
    db.commit()
    db.refresh(project)
    events_svc.record_user(db, user, action="update_project", target_type="project",
                           target_id=project_id, project_id=project_id,
                           meta={"fields": sorted(changed.keys())})
    return project


@router.post("/{project_id}/retag", response_model=ProjectOut)
def retag(
    project_id: str,
    body: ProjectRetagIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change a project's tag. Same gate as any other project setting — no new tier."""
    authz.require_writable(db, user.id, project_id)
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    was = project.tag
    try:
        project = projects_svc.retag_project(db, project_id, body.tag)
    except ValueError as e:
        raise HTTPException(422, str(e))
    events_svc.record_user(db, user, action="retag_project", target_type="project",
                           target_id=project_id, project_id=project_id,
                           meta={"from": was, "to": project.tag})
    return project


@router.get("/{project_id}/counts")
def project_counts(project_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """The badge numbers the app shell renders — a few integers instead of the four full
    collections it used to fetch on every route (GRPH-431)."""
    authz.require_readable(db, user.id, project_id)
    return projects_svc.shell_counts(db, project_id)


def _load_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def _require_gitops_read(db: Session, user: User, project: Project) -> None:
    if settings.hosted_mode:
        if not project.org_id:
            raise HTTPException(404, "project not found")
        authz.require_org_member(db, user.id, project.org_id)
        return
    authz.require_readable(db, user.id, project.id)


@router.get("/{project_id}/gitops", response_model=GitopsView)
def get_gitops(project_id: str, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    project = _load_project(db, project_id)
    _require_gitops_read(db, user, project)
    view = gitops.resolve(db, project_id)
    return gitops.fill_writable(view, db, user.id)


@router.patch("/{project_id}/gitops", response_model=GitopsView)
def patch_gitops(project_id: str, body: GitopsPatch, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    project = _load_project(db, project_id)
    if settings.hosted_mode:
        if not project.org_id:
            raise HTTPException(404, "project not found")
        authz.require_org_admin(db, user.id, project.org_id)
    else:
        if code_sync.link_status(db)["linked"]:
            raise HTTPException(403, gitops.LINKED_PATCH_DETAIL)
        authz.require_writable(db, user.id, project_id)
    try:
        changed = gitops.apply_patch(project, {k: getattr(body, k) for k in body.model_fields_set})
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.commit()
    db.refresh(project)
    events_svc.record_user(
        db, user, action="update_gitops", target_type="project", target_id=project_id,
        project_id=project_id, meta={"fields": changed},
    )
    view = gitops.resolve(db, project_id)
    return gitops.fill_writable(view, db, user.id)


@router.get("/{project_id}/members", response_model=list[MemberOut])
def list_members(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    rows = db.scalars(select(Membership).where(Membership.project_id == project_id)).all()
    out = []
    for m in rows:
        user = db.get(User, m.user_id)
        if user is not None:
            out.append(MemberOut(user=UserOut.model_validate(user), role=m.role, access=m.access))
    return out
