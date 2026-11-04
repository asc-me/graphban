from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import Refused
from app.mcp_server import manifest_tool_count
from app.models import ApiKey, Project, User
from app.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyRotate
from app.security import authz
from app.security.apikey import generate_api_key
from app.security.deps import get_current_user
from app.services import events as events_svc

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _out(row: ApiKey) -> ApiKeyOut:
    """A key as the registry shows it, with the size of the manifest it is actually shipped —
    computed here rather than stored, so a change to what is core is reflected on every
    existing key the moment it deploys."""
    out = ApiKeyOut.model_validate(row)
    out.tool_count = manifest_tool_count(row)
    return out


@router.get("", response_model=list[ApiKeyOut])
def list_keys(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return [_out(row) for row in db.scalars(select(ApiKey).where(ApiKey.user_id == user.id))]


@router.post("", response_model=ApiKeyCreated, status_code=201)
def create_key(body: ApiKeyCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if body.project_id is not None:
        if db.get(Project, body.project_id) is None:
            raise HTTPException(422, f"unknown project: {body.project_id!r}")
        # A key inherits its power from its owner's memberships; minting one for a
        # project the owner can't read would only produce a dead key.
        authz.require_readable(db, user.id, body.project_id)
    if "sync" in body.scopes or "gate" in body.scopes:
        # A sync credential MUST pin to one project (AL-219 D6). Left global, the
        # `key_sync_ids` fallback would resolve the ingest target to EVERY project its
        # owner can write — so one leaked key distributed to a Cursor Team could push a
        # code graph into all of them. Pinning is the blast radius.
        #
        # Gate keys get the same treatment (GRPH-580). `key_gate_ids` falls back to every
        # writable project when `project_id` is null, so one leaked CI secret would attest
        # completions across all of them. Sync already refused to be global for this
        # reason; gate was not given the same rule.
        kind = "gate" if "gate" in body.scopes else "sync"
        if body.project_id is None:
            raise HTTPException(422, f"a {kind!r} credential must target one project")
        # Ingest / attestation writes; require_readable above is not enough. Without this
        # a key minted on a read-only project mints fine and then 403s at use time.
        authz.require_writable(db, user.id, body.project_id)
    row, plaintext = generate_api_key(
        db, user.id, body.name, body.scopes, body.project_id, body.expires_in_days,
        tool_tiers=body.tool_tiers, origin=body.origin,
    )
    events_svc.record_user(db, user, action="create_api_key", target_type="api_key",
                           target_id=row.id, project_id=row.project_id,
                           meta={"name": row.name, "scopes": row.scopes,
                                 "tool_tiers": row.tool_tiers})
    out = ApiKeyCreated.model_validate({**_out(row).model_dump(), "plaintext": plaintext})
    return out


@router.delete("/{key_id}", status_code=204)
def revoke_key(key_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Revoke a credential. SOFT — the row stays and `revoked` is set (GRPH-788).

    This deleted the row until a key that had ever been USED became undeletable. PRD-38 added
    `agent_calls.api_key_id` as a NOT NULL foreign key with no `ondelete`, so `db.delete`
    raised `IntegrityError` on commit and the route 500'd — for every key with call history,
    which is every key worth revoking. An unused key deleted fine, so the failure arrived
    exactly backwards, and the Settings trash button reported nothing at all.

    Soft is the right answer rather than a cascade, and the codebase had already decided so
    twice: `end_wave` and `revoke-expired` both set this flag, and `verify_api_key` refuses a
    revoked key outright, so this is a real revocation and not a cosmetic one. Keeping the row
    also keeps the telemetry the foreign key exists to protect — a deletion would have to
    orphan or destroy the call history that answers "what did this credential do".

    Idempotent: revoking an already-revoked key succeeds and records the event again, because
    the caller asked for a state and that state is what it gets.
    """
    row = db.get(ApiKey, key_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "key not found")
    project_id, name = row.project_id, row.name
    row.revoked = True
    db.commit()
    events_svc.record_user(db, user, action="revoke_api_key", target_type="api_key",
                           target_id=key_id, project_id=project_id, meta={"name": name})


ROTATE_RULE = "keys.rotate_only_where_it_replaces"
_RERUN = {"gban_setup": "gban setup", "gban_swamp_setup": "gban swamp setup --replace"}


@router.post("/{key_id}/rotate", response_model=ApiKeyCreated, status_code=201)
def rotate_key(key_id: str, body: ApiKeyRotate, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """Replace a credential with an identical one and revoke the old, in ONE transaction.

    The replacement is flushed, the old row is marked revoked, and a single commit publishes
    both — so a failure between them leaves neither change behind. The browser never builds
    this out of create plus the soft delete, which could strand a user with no working key.
    """
    old = db.get(ApiKey, key_id)
    if old is None or old.user_id != user.id:
        raise HTTPException(404, "key not found")
    if old.revoked:
        raise Refused(409, "key is already revoked", rule_id=ROTATE_RULE)
    if "sync" in (old.scopes or []):
        raise Refused(409, "a link credential is relinked, not rotated", rule_id=ROTATE_RULE)
    if old.fleet_wave:
        raise Refused(409, "a fleet wave key is not rotated; end the wave instead", rule_id=ROTATE_RULE)
    if old.origin in _RERUN:
        raise Refused(409, f"this key was minted by `{_RERUN[old.origin]}`; rerun that command",
                      rule_id=ROTATE_RULE)
    if old.project_id is not None:
        # Same guards as create: a user who lost access must not rotate into a dead key.
        authz.require_readable(db, user.id, old.project_id)
        if "gate" in (old.scopes or []):
            authz.require_writable(db, user.id, old.project_id)
    try:
        row, plaintext = generate_api_key(
            db, user.id, old.name, list(old.scopes or []), old.project_id, body.expires_in_days,
            tool_tiers=list(old.tool_tiers) if old.tool_tiers else None, origin=old.origin,
            roles=list(old.roles or []), posture=old.posture, commit=False)
        old.revoked = True
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    events_svc.record_user(db, user, action="rotate_api_key", target_type="api_key",
                           target_id=old.id, project_id=row.project_id,
                           meta={"name": row.name, "new_id": row.id})
    return ApiKeyCreated.model_validate({**_out(row).model_dump(), "plaintext": plaintext})
