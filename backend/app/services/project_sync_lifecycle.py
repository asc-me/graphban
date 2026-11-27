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


# ---- the console's choice and the effective level (GRPH-1978, GBC-P23) ---------------------

#: What the box reports on ``POST /api/sync/project-sync``. Only ``full`` lets holders out.
EFFECTIVE_FULL = "full"
EFFECTIVE_SUMMARIES = "summaries"


def effective_sync(mapping: SyncProject | None) -> str:
    """``full`` only when the console chose ``full`` AND the box admin chose ``sync``.

    The console narrows, never widens (owner, 2026-11-27): console ``full`` on a box at
    ``digests`` is ``summaries``. A null console choice is ``summaries``.
    """
    if mapping is None:
        return EFFECTIVE_SUMMARIES
    if mapping.console_sync == EFFECTIVE_FULL and mapping.sync_level == outbox.SYNC_LEVEL_SYNC:
        return EFFECTIVE_FULL
    return EFFECTIVE_SUMMARIES


def holders_on(mapping: SyncProject | None) -> bool:
    """May this project's pushes carry ``assignee`` / ``held_by``?

    Both the effective level and the level the console last stored must be ``full``. The
    second half is what makes "report before the first holder push" structural: a holder
    field cannot go out ahead of the 200 that stored ``full``.
    """
    return (mapping is not None and effective_sync(mapping) == EFFECTIVE_FULL
            and mapping.reported_sync == EFFECTIVE_FULL)


def non_home(mapping: SyncProject | None) -> bool:
    """The console said explicitly that this box is not the project's home.

    A missing ``home`` is null and is NOT this: such a box pushes as before and the
    console's ``403 not_home`` still decides.
    """
    return mapping is not None and mapping.console_home is False


def note_console_choice(db: Session, rows: list[dict]) -> None:
    """Store each mapped project's ``sync`` and ``home`` from ``GET /api/sync/projects``.

    ``rows`` are the console's entries, keyed by ``id`` (the cloud project id). A mapping the
    console did not list keeps what it had: absence from one read is not a change of choice.
    A ``sync`` key that is null or anything but ``full`` is stored as ``summaries`` — what it
    counts as. An entry with no ``sync`` key at all is a console that predates the choice:
    stored null, and nothing is reported for it. A ``home`` that is not a bool is null
    (unknown), never false.
    """
    by_cloud = {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id")}
    changed = False
    for mapping in db.scalars(select(SyncProject)).all():
        row = by_cloud.get(mapping.cloud_project_id or "")
        if row is None:
            continue
        if "sync" in row:
            sync = EFFECTIVE_FULL if row["sync"] == EFFECTIVE_FULL else EFFECTIVE_SUMMARIES
        else:
            sync = None
        home = row.get("home")
        home = home if isinstance(home, bool) else None
        if mapping.console_sync != sync or mapping.console_home is not home:
            mapping.console_sync = sync
            mapping.console_home = home
            changed = True
    if changed:
        db.commit()


def record_refusal(db: Session, mapping: SyncProject | None, what: str, *,
                   reason: str | None, field: str | None = None,
                   now: datetime | None = None) -> None:
    """A 403 or 422 on a GBC-P23 push, written where link status reads it.

    ``last_push_result`` / ``last_push_reason`` are what ``GET /api/sync/status`` shows per
    mapping. Swallowing the refusal would leave the panel saying ``ok`` while nothing landed.
    """
    if mapping is None:
        return
    detail = reason or "refused"
    if field:
        detail = f"{detail} ({field})"
    mapping.last_push_at = _now(now)
    mapping.last_push_result = "refused"
    mapping.last_push_reason = f"{what}: {detail}"
    db.commit()


def report(db: Session, mapping: SyncProject, *, transport=None,
           now: datetime | None = None, force: bool = False) -> str | None:
    """Report this mapping's effective level when it differs from what the console stored.

    Returns the level the console now holds for this box (``reported_sync``), or None when
    nothing has been stored yet. A refusal is recorded on link status; no answer leaves
    ``reported_sync`` as it was, so holders stay off until a 200 says ``full``. When the
    stored level moves on a synced project, its entities are requeued: their bodies gain or
    lose the holder fields.
    """
    if not mapping.cloud_project_id:
        return mapping.reported_sync
    if mapping.console_sync is None and mapping.reported_sync is None:
        # The console has not stated a choice (not read yet, or it predates GBC-P23).
        # There is nothing to answer, and an older console does not serve the route.
        return None
    wanted = effective_sync(mapping)
    if mapping.reported_sync == wanted and not force:
        return wanted
    project_id = mapping.local_project_id
    result = client.report_effective_sync(
        db, cloud_project_id=mapping.cloud_project_id, effective_sync=wanted,
        transport=transport)
    mapping = db.get(SyncProject, project_id)
    if mapping is None:
        return None
    if result.status == "refused":
        record_refusal(db, mapping, "project-sync", reason=result.reason, field=result.field,
                       now=now)
        return mapping.reported_sync
    if result.status != "ok":
        return mapping.reported_sync
    moved = mapping.reported_sync != result.version
    mapping.reported_sync = result.version
    if moved and mapping.sync_level == outbox.SYNC_LEVEL_SYNC:
        _resume(db, project_id, _now(now))
    db.commit()
    return mapping.reported_sync


def report_all(db: Session, *, transport=None, now: datetime | None = None,
               force: bool = False) -> dict[str, str | None]:
    """``report`` for every mapping with a cloud project. Keyed by local project id."""
    rows = list(db.scalars(select(SyncProject).order_by(SyncProject.local_project_id)).all())
    return {row.local_project_id: report(db, row, transport=transport, now=now, force=force)
            for row in rows if row.cloud_project_id}


def refresh(db: Session, *, transport=None, now: datetime | None = None) -> dict | None:
    """Re-read the console's choice and re-report every mapping. The holder-422 path.

    Returns ``report_all``'s answer, or None when the projects read got no ``ok``: the
    stored choice then stands, and so does the level the console last stored.
    """
    from app.services import code_sync

    try:
        read = code_sync.console_projects(db)
    except code_sync.NotLinked:
        return None
    if read.get("status") != "ok":
        return None
    # Forced: the 422 is the console saying its stored level is not the one this box
    # believes it reported, so "already reported" is exactly the fact in doubt.
    return report_all(db, transport=transport, now=now, force=True)
