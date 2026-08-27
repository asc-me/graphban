import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
from app.services import credential_retry
from app.services import platform as platform_svc

router = APIRouter(prefix="/platform", tags=["platform"])


def _sync_root(project_id: str, folder: str) -> str:
    sub = (folder or project_id).strip().strip("/").replace("..", "") or project_id
    return os.path.join(settings.sync_dir, sub)


@router.get("/providers")
def list_providers(_: User = Depends(get_current_user)):
    """The AI-provider catalog the Settings UI renders (id, label, kind, embeds, defaults)."""
    return {"providers": provider_registry.PROVIDERS}


@router.get("/credentials")
def list_credentials(project_id: str = "core", db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Every credential configured on this deployment, with state and derived `used-by`.

    Scoped through a PROJECT the caller can read rather than taking a scope directly: the
    scope is an org id under hosted multi-tenancy, and letting a caller name one would be a
    cross-tenant read with extra steps. Resolving it from a project the authz layer already
    vetted means the existing guard is the only guard, and there is no second path to keep
    in step with the first.
    """
    authz.require_readable(db, user.id, project_id)
    return {"credentials": platform_svc.list_credentials(
        db, platform_svc.scope_for(db, project_id))}


class CredentialIn(BaseModel):
    kind: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class CredentialPatch(BaseModel):
    kind: str | None = None
    label: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ScopeDefaultsIn(BaseModel):
    """`None` clears; an omitted field is left alone. Those are different intentions, so the
    sentinel is absence-of-key rather than null."""

    default_credential_id: str | None = None
    fallback_credential_id: str | None = None
    embed_credential_id: str | None = None


class ProjectCredentialIn(BaseModel):
    credential_id: str | None = None
    model_override: str | None = None


def _scope(db: Session, user: User, project_id: str) -> str:
    """Resolve the caller's scope through a project the authz layer has already vetted.

    Never from a scope the caller names: a scope is an org id under hosted multi-tenancy, and
    accepting one directly would be a cross-tenant write with extra steps.
    """
    authz.require_writable(db, user.id, project_id)
    return platform_svc.scope_for(db, project_id)


@router.post("/credentials", status_code=201)
def create_credential(body: CredentialIn, project_id: str = "core",
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    scope = _scope(db, user, project_id)
    if body.kind not in provider_registry.IDS:
        raise HTTPException(422, f"unknown provider: {body.kind}")
    try:
        cred = platform_svc.create_credential(
            db, scope, kind=body.kind, label=body.label, base_url=body.base_url,
            api_key=body.api_key, model=body.model)
    except ValueError as e:
        # 422, not 500: the provider answered and said no. GRPH-485's rule, unchanged.
        raise HTTPException(422, str(e)) from None
    return {"id": cred.id, "state": cred.state}


@router.patch("/credentials/{credential_id}")
def update_credential(credential_id: str, body: CredentialPatch, project_id: str = "core",
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    scope = _scope(db, user, project_id)
    try:
        cred = platform_svc.update_credential(db, credential_id, scope,
                                              **body.model_dump(exclude_unset=True))
    except LookupError:
        raise HTTPException(404, "no such credential") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"id": cred.id, "state": cred.state}


@router.delete("/credentials/{credential_id}", status_code=204)
def delete_credential(credential_id: str, project_id: str = "core",
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    scope = _scope(db, user, project_id)
    try:
        platform_svc.delete_credential(db, credential_id, scope)
    except LookupError:
        raise HTTPException(404, "no such credential") from None
    except platform_svc.CredentialInUse as e:
        # 409 naming every referencing project and role — see the service docstring for why a
        # bare "in use" is not good enough.
        raise HTTPException(409, str(e)) from None
    return None


@router.post("/credentials/{credential_id}/retry")
def retry_credential(credential_id: str, project_id: str = "core",
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """`Test connection` — re-ask a credential now, through the same claim the loop uses.

    The operator presses this exactly when a row looks stuck, which is when the background
    loop is also most likely to be picking it up. Going through `claim` means the budget
    decrements once whoever wins, rather than twice for one real attempt.

    Returns the resulting `state` rather than a bare 204: the whole point of pressing it is to
    find out what happened, and making the caller re-read the list to discover that is a round
    trip for information this response already has.
    """
    scope = _scope(db, user, project_id)
    try:
        state = credential_retry.retry_now(db, credential_id, scope)
    except LookupError:
        raise HTTPException(404, "no such credential") from None
    cred = platform_svc.credential_in_scope(db, credential_id, scope)
    return {"id": credential_id, "state": state,
            "last_error": cred.last_error if cred else "",
            "validation_attempts": cred.validation_attempts if cred else 0}


@router.put("/credentials/defaults")
def set_defaults(body: ScopeDefaultsIn, project_id: str = "core",
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    scope = _scope(db, user, project_id)
    sent = body.model_dump(exclude_unset=True)
    kwargs = {k: sent.get(k) for k in sent}
    try:
        row = platform_svc.set_scope_defaults(db, scope, **kwargs)
    except LookupError:
        raise HTTPException(404, "no such credential") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"scope": row.scope, "default_credential_id": row.default_credential_id,
            "fallback_credential_id": row.fallback_credential_id,
            "embed_credential_id": row.embed_credential_id}


@router.put("/credentials/project")
def set_project_credential(body: ProjectCredentialIn, project_id: str = "core",
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    _scope(db, user, project_id)
    sent = body.model_dump(exclude_unset=True)
    try:
        project = platform_svc.set_project_credential(
            db, project_id, **{k: sent.get(k) for k in sent})
    except LookupError:
        raise HTTPException(404, "no such credential or project") from None
    return {"project_id": project.id, "credential_id": project.credential_id,
            "model_override": project.model_override}


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
