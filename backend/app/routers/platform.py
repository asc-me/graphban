import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.schemas import (
    GdriveConnectIn,
    GithubConnectIn,
    GithubIssueIn,
    PlatformConfigOut,
    PlatformUpdate,
)
from app.providers import registry as provider_registry
from app.security import authz
from app.security.deps import get_current_user
from app.services import drive_sync
from app.services import events as events_svc
from app.services import items as items_svc
from app.services import platform as platform_svc

router = APIRouter(prefix="/platform", tags=["platform"])


def _sync_root(project_id: str, folder: str) -> str:
    sub = (folder or project_id).strip().strip("/").replace("..", "") or project_id
    return os.path.join(settings.sync_dir, sub)


@router.get("/providers")
def list_providers(_: User = Depends(get_current_user)):
    """The AI-provider catalog the Settings UI renders (id, label, kind, embeds, defaults)."""
    return {"providers": provider_registry.PROVIDERS}


@router.get("", response_model=PlatformConfigOut)
def get_platform(project_id: str = "core", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    return platform_svc.get_config(db, project_id)


@router.patch("", response_model=PlatformConfigOut)
def update_platform(body: PlatformUpdate, project_id: str = "core", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, project_id)
    events_svc.record_user(db, user, action="update_platform_config", target_type="project",
                           target_id=project_id, project_id=project_id,
                           meta={"fields": sorted(body.model_dump(exclude_unset=True).keys())})
    if body.llm_mode is not None and body.llm_mode not in ("stub", "local", "cloud"):
        raise HTTPException(422, "llm_mode must be stub | local | cloud")
    if body.active_chat_provider and body.active_chat_provider not in provider_registry.IDS:
        raise HTTPException(422, f"unknown provider: {body.active_chat_provider}")
    try:
        return platform_svc.update_config(db, project_id, body.model_dump(exclude_unset=True))
    except platform_svc.UnknownModel as e:
        # 422, not 500: the config is wrong, not the server. The message names what the
        # provider does have, so fixing it is one edit rather than a hunt (GRPH-485).
        raise HTTPException(422, str(e))


@router.post("/github/connect", response_model=PlatformConfigOut)
def github_connect(body: GithubConnectIn, project_id: str = "core", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, project_id)
    return platform_svc.connect_github(db, project_id, account=body.account, repo=body.repo)


@router.post("/github/disconnect", response_model=PlatformConfigOut)
def github_disconnect(project_id: str = "core", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, project_id)
    return platform_svc.disconnect_github(db, project_id)


@router.post("/github/create-issue")
def github_create_issue(
    body: GithubIssueIn, project_id: str = "core", db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Create a tracker item mirroring a GitHub issue.

    Local slice: records the item and the intended issue. Pushing to the real
    GitHub API requires a connected account with a token (out of scope offline).
    """
    authz.require_writable(db, user.id, project_id)
    cfg = platform_svc.get_config(db, project_id)
    item = items_svc.create_item(
        db, title=body.title, description=body.body, tags=["github", body.type],
        project_id=project_id, reporter={"name": user.name, "handle": user.handle, "avatar": user.avatar},
    )
    return {
        "item": {"id": item.id, "title": item.title},
        "pushed_to_github": False,
        "detail": (
            f"Would open an issue in {cfg.github_repo}" if cfg.github_connected
            else "GitHub not connected — item created locally only"
        ),
    }


@router.post("/gdrive/connect", response_model=PlatformConfigOut)
def gdrive_connect(body: GdriveConnectIn, project_id: str = "core", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, project_id)
    return platform_svc.connect_gdrive(db, project_id, account=body.account, folder=body.folder)


@router.post("/gdrive/disconnect", response_model=PlatformConfigOut)
def gdrive_disconnect(project_id: str = "core", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, project_id)
    return platform_svc.disconnect_gdrive(db, project_id)


@router.post("/gdrive/sync")
def gdrive_sync(project_id: str = "core", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Two-way sync of this project's PRDs with the connected folder's PRDs/ subdirectory."""
    authz.require_writable(db, user.id, project_id)
    cfg = platform_svc.get_config(db, project_id)
    if not cfg.gdrive_connected:
        raise HTTPException(400, "Google Drive is not connected for this project")
    root = _sync_root(project_id, cfg.gdrive_folder)
    report = drive_sync.sync(db, project_id, root_dir=root)
    return {"folder": root, "prds_dir": os.path.join(root, drive_sync.PRDS_SUBDIR), **report}
