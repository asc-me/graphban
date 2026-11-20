"""Unsync, purge, and the level change a box admin records (GRPH-1738, P89).

``set_level`` is the only way a project's ``sync_level`` changes. Leaving ``sync``
stops capture and clears that project's outbox. ``purge`` (the default) calls
``DELETE /api/sync/project-entities`` and clears ``sync_manifest`` only after the
console confirms with 200. ``unknown`` — no answer, including a timeout — stores
``pending_purge`` and leaves the manifest. It does not report the purge as done.
A later ``retry_pending_purges`` tries those rows again.

Local only is the absence of a ``sync_project`` row, not a third stored level.
The row stays while a purge of it is still unconfirmed, so the marker has
somewhere to live. Unlink itself is ``link_device.clear``: it drops the
credential and the mappings and keeps the manifest.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Project, SyncManifest, SyncOutbox, SyncProject, User
from app.services import project_sync_client as client
from app.services import project_sync_outbox as outbox
from app.services import project_sync_payload as payload

#: Not stored on the row. Applying it deletes the mapping once any purge has
#: been confirmed, or immediately when the caller declined the purge.
LEVEL_LOCAL = "local"
_LEVELS = (outbox.SYNC_LEVEL_DIGESTS, outbox.SYNC_LEVEL_SYNC, LEVEL_LOCAL)


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _actor(user: User) -> str:
    """Who changed the level. The email is the name the audit shows; the id is
    only the fallback when the account has none."""
    email = (user.email or "").strip()
    return email or user.id


def _require_admin(db: Session, user: User, action: str) -> None:
    # The router's check, not a second copy of it. A service that grew its own
    # admin test would pass this module's tests while the rule the product
    # names was no longer the one being called.
    from app.routers.sync import refuse_unless_box_admin

    refuse_unless_box_admin(db, user, action)


def _audit(mapping: SyncProject, user: User, now: datetime) -> None:
    mapping.enabled_by = _actor(user)
    mapping.enabled_at = now


def _answer(project_id: str, level: str, *, purged: bool, pending: bool,
            reason: str | None = None, deleted: int | None = None) -> dict:
    out = {
        "project_id": project_id,
        "level": level,
        "purged": purged,
        "pending_purge": pending,
    }
    if reason is not None:
        out["reason"] = reason
    if deleted is not None:
        out["deleted"] = deleted
    return out


def _clear_outbox(db: Session, project_id: str) -> None:
    db.execute(delete(SyncOutbox).where(SyncOutbox.project_id == project_id))


def _clear_manifest(db: Session, project_id: str) -> None:
    db.execute(delete(SyncManifest).where(SyncManifest.project_id == project_id))


def _resume(db: Session, project_id: str, now: datetime) -> int:
    """Queue entities whose hash is not the confirmed one.

    An unchanged entity is not queued. The flush then has nothing to send for
    it, which is the relink skipping a full re-backfill. ``None`` from
    ``backfill`` means the project itself is missing — not an empty project —
    and queues nothing rather than reporting it caught up.
    """
    records = payload.backfill(db, project_id)
    if not records:
        return 0
    queued = 0
    for record in records:
        manifest = db.get(SyncManifest, (project_id, record["type"], record["id"]))
        if manifest is not None and manifest.hash == record["hash"]:
            stale = db.get(SyncOutbox, (project_id, record["type"], record["id"]))
            if stale is not None:
                db.delete(stale)
            continue
        outbox.capture(
            db, project_id=project_id, entity_type=record["type"], entity_id=record["id"],
            change_class=outbox.CHANGE_EDIT, action="resume", now=now,
        )
        queued += 1
    return queued


def _finish_purge(db: Session, mapping: SyncProject, requested: str, *,
                  transport) -> dict:
    """One DELETE. The manifest moves only inside the ``ok`` branch.

    Clearing it before that branch is what
    ``test_a_timeout_keeps_the_manifest_until_the_purge_is_confirmed`` exists
    to catch: a timeout would then look the same as a confirmed purge.
    """
    project_id = mapping.local_project_id
    result = client.purge_entities(
        db, project=mapping.cloud_project_id, transport=transport)
    mapping = db.get(SyncProject, project_id)
    if mapping is None:
        # The row held the marker. Without it a missing answer cannot be retried,
        # so this is not reported as purged.
        return _answer(project_id, requested, purged=False, pending=False,
                       reason="the mapping was removed before the purge was confirmed")
    if result.status == "ok":
        _clear_manifest(db, project_id)
        if requested == LEVEL_LOCAL:
            db.delete(mapping)
            db.commit()
            return _answer(project_id, LEVEL_LOCAL, purged=True, pending=False,
                           deleted=result.deleted)
        mapping.pending_purge = None
        mapping.sync_level = outbox.SYNC_LEVEL_DIGESTS
        db.commit()
        return _answer(project_id, outbox.SYNC_LEVEL_DIGESTS, purged=True, pending=False,
                       deleted=result.deleted)
    if result.status == "unknown":
        mapping.pending_purge = requested
        db.commit()
        return _answer(project_id, requested, purged=False, pending=True, reason=result.reason)
    mapping.pending_purge = None
    db.commit()
    return _answer(project_id, mapping.sync_level, purged=False, pending=False,
                   reason=result.reason)


def set_level(db: Session, project_id: str, level: str, purge: bool = True, *,
              user: User, now: datetime | None = None, transport=None) -> dict:
    """Move a project between ``sync``, ``digests``, and local only.

    ``purged`` is true only after DELETE returns 200. ``pending_purge`` is true
    when the console gave no answer; the manifest is still there and
    ``retry_pending_purges`` will try again. A box admin is required. The
    change records ``enabled_by`` and ``enabled_at``.
    """
    if level not in _LEVELS:
        raise ValueError(
            f"invalid sync level: {level!r} — one of {', '.join(_LEVELS)}")
    _require_admin(db, user, "change the sync level for")
    if db.get(Project, project_id) is None:
        raise ValueError(f"unknown project: {project_id!r}")
    moment = _now(now)
    mapping = db.get(SyncProject, project_id)

    if level == outbox.SYNC_LEVEL_SYNC:
        if mapping is None or not mapping.cloud_project_id:
            raise ValueError("a local-only project has no cloud project to sync")
        mapping.sync_level = outbox.SYNC_LEVEL_SYNC
        # Re-opting in cancels a purge that never confirmed. The replica is
        # still there; deleting it later would throw away the sync just turned on.
        mapping.pending_purge = None
        _audit(mapping, user, moment)
        _resume(db, project_id, moment)
        db.commit()
        return _answer(project_id, level, purged=False, pending=False)

    if level == outbox.SYNC_LEVEL_DIGESTS and mapping is None:
        raise ValueError("a local-only project has no mapping to set to digests")

    previous = mapping.sync_level if mapping is not None else LEVEL_LOCAL
    pending = (mapping.pending_purge or "") if mapping is not None else ""
    leaving_sync = previous == outbox.SYNC_LEVEL_SYNC
    should_purge = bool(
        purge and mapping is not None and mapping.cloud_project_id
        and (leaving_sync or pending or (level == LEVEL_LOCAL and previous != LEVEL_LOCAL))
    )

    if mapping is not None and (leaving_sync or level != previous or pending):
        _audit(mapping, user, moment)
        _clear_outbox(db, project_id)
        # Capture reads sync_level. Anything but sync stops it, including the
        # hold while a local-only purge is still unconfirmed.
        if previous == outbox.SYNC_LEVEL_SYNC or level == outbox.SYNC_LEVEL_DIGESTS:
            mapping.sync_level = outbox.SYNC_LEVEL_DIGESTS
        if should_purge:
            mapping.pending_purge = level
        db.commit()
        mapping = db.get(SyncProject, project_id)

    if not should_purge:
        if level == LEVEL_LOCAL and mapping is not None:
            db.delete(mapping)
            db.commit()
            return _answer(project_id, LEVEL_LOCAL, purged=False, pending=False)
        if mapping is not None:
            db.commit()
        stored = LEVEL_LOCAL if db.get(SyncProject, project_id) is None else (
            db.get(SyncProject, project_id).sync_level)
        return _answer(project_id, stored, purged=False, pending=False)

    return _finish_purge(db, mapping, level, transport=transport)


def set_cloud_permission(db: Session, project_id: str, permission: str, *,
                         user: User, now: datetime | None = None) -> dict:
    """Change what the cloud may propose. Box admin, and the same audit columns
    as a level change. A local-only project has no row to record it on."""
    if permission not in outbox.CLOUD_PERMISSIONS:
        raise ValueError(
            f"invalid cloud permission: {permission!r} — one of "
            f"{', '.join(outbox.CLOUD_PERMISSIONS)}")
    _require_admin(db, user, "change the cloud permission for")
    mapping = db.get(SyncProject, project_id)
    if mapping is None:
        raise ValueError("a local-only project has no cloud permission to set")
    moment = _now(now)
    mapping.cloud_permission = permission
    _audit(mapping, user, moment)
    enabled_by, enabled_at = mapping.enabled_by, mapping.enabled_at
    db.commit()
    return {
        "project_id": project_id,
        "cloud_permission": permission,
        "enabled_by": enabled_by,
        "enabled_at": enabled_at,
    }


def retry_pending_purges(db: Session, *, transport=None) -> list[dict]:
    """Try every purge the console has not confirmed.

    A row with no marker is not tried. An empty result is "nothing was owed",
    not "every purge succeeded".
    """
    rows = list(db.scalars(select(SyncProject).where(
        SyncProject.pending_purge.is_not(None),
        SyncProject.pending_purge != "",
    )).all())
    out = []
    for row in rows:
        requested = row.pending_purge
        if requested not in (outbox.SYNC_LEVEL_DIGESTS, LEVEL_LOCAL):
            continue
        out.append(_finish_purge(db, row, requested, transport=transport))
    return out
