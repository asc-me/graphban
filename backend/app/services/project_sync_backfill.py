"""Resumable initial backfill (GRPH-1736, P89 S4).

When a project moves to ``sync``, every item, PRD, comment and wait is seeded into
the outbox once. The flusher (GRPH-1735) sends those rows under the same budget.
This module does not push.

A ``200`` that stored the entity is the only confirmation. ``SyncManifest`` is that
record: the flusher writes it, and a later ``start_backfill`` does not reseed an
entity whose manifest hash is still the body's hash. A timeout, a ``429``, a
``5xx``, or a per-entity refusal leaves no matching manifest row, so the entity
stays unconfirmed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Project, SyncManifest, SyncOutbox, SyncProject
from app.services.project_sync_client import register_project
from app.services.project_sync_outbox import CHANGE_NEW, SYNC_LEVEL_SYNC, capture
from app.services.project_sync_payload import backfill as list_entities

#: A parked row whose reason is one of these was refused as an entity. Anything
#: else that parks the remainder (``contract``, ``not_home``) refused the call.
_ENTITY_PARKED = frozenset({"too_large", "invalid", "forbidden_type"})


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _key(project_id: str, record: dict) -> tuple[str, str, str]:
    return project_id, record["type"], record["id"]


def _confirmed(manifest: SyncManifest | None, record: dict) -> bool:
    """True only when the console's stored hash is still this body.

    A missing row has never been confirmed. An empty hash is not a confirmation
    of an empty body: the payload hash is ``sha256:`` plus a digest, and a blank
    manifest hash does not equal it.
    """
    return manifest is not None and bool(manifest.hash) and manifest.hash == record["hash"]


def _parked_reason(row: SyncOutbox | None) -> str:
    if row is None:
        return ""
    return (row.parked_reason or "").strip()


def _state(total: int, confirmed: int, pending: int, reasons: list[str]) -> str:
    if confirmed == total:
        return "done"
    if pending or not reasons:
        # Unconfirmed and not parked: still to send, or not seeded yet.
        # Both are running. Neither is done — nothing has been confirmed.
        return "running"
    if any(reason in _ENTITY_PARKED for reason in reasons):
        return "parked"
    return "refused"


def backfill_status(db: Session, project_id: str) -> dict:
    """``{total, confirmed, parked, state}`` from the manifest and the outbox.

    ``state`` is ``running``, ``done``, ``parked``, or ``refused``. ``done`` is
    ``confirmed == total`` after the project was found. A missing project is
    ``refused`` with ``total`` 0: the same numbers on a project that exists and
    has nothing to send are ``done``, and the state is what keeps the two apart.
    """
    records = list_entities(db, project_id)
    if records is None:
        return {"total": 0, "confirmed": 0, "parked": 0, "state": "refused"}
    confirmed = parked = pending = 0
    reasons: list[str] = []
    for record in records:
        manifest = db.get(SyncManifest, _key(project_id, record))
        if _confirmed(manifest, record):
            confirmed += 1
            continue
        reason = _parked_reason(db.get(SyncOutbox, _key(project_id, record)))
        if reason:
            parked += 1
            reasons.append(reason)
        else:
            pending += 1
    return {
        "total": len(records),
        "confirmed": confirmed,
        "parked": parked,
        "state": _state(len(records), confirmed, pending, reasons),
    }


def _cloud_id(mapping: SyncProject | None) -> str:
    if mapping is None:
        return ""
    return (mapping.cloud_project_id or "").strip()


def _store_cloud(db: Session, project: Project, *, cloud_project_id: str, tag: str,
                 now: datetime) -> None:
    """Remember the replica and move the project to ``sync``. Does not commit."""
    row = db.get(SyncProject, project.id)
    if row is None:
        row = SyncProject(
            local_project_id=project.id,
            cloud_project_id=cloud_project_id,
            cloud_project_tag=tag,
            sync_level=SYNC_LEVEL_SYNC,
            enabled_at=now,
        )
        db.add(row)
    else:
        # An id already stored is the replica. Replacing it would rename the link.
        if not (row.cloud_project_id or "").strip():
            row.cloud_project_id = cloud_project_id
            row.cloud_project_tag = tag
        row.sync_level = SYNC_LEVEL_SYNC
        if row.enabled_at is None:
            row.enabled_at = now
    db.flush()


def _seed(db: Session, project_id: str, records: list[dict], now: datetime) -> tuple[int, int]:
    """Queue entities the manifest does not already confirm. Returns (seeded, skipped)."""
    seeded = skipped = 0
    for record in records:
        manifest = db.get(SyncManifest, _key(project_id, record))
        # Unchanged and already confirmed: do not reseed. Deleting this consult
        # puts the entity back on the outbox and fails
        # test_a_restart_sends_no_entity_the_manifest_already_confirmed.
        if _confirmed(manifest, record):
            skipped += 1
            continue
        if db.get(SyncOutbox, _key(project_id, record)) is not None:
            # Already queued. Capturing again would bump the outbox version and
            # the flusher would treat the row as a newer edit than the one it read.
            continue
        row = capture(
            db, project_id=project_id, entity_type=record["type"], entity_id=record["id"],
            change_class=CHANGE_NEW, action="backfill", now=now,
        )
        if row is not None:
            seeded += 1
    return seeded, skipped


def start_backfill(db: Session, project_id: str, create_in_org: bool = False, *,
                   transport=None, now: datetime | None = None) -> dict:
    """Seed the outbox for one project. The flusher sends it.

    ``create_in_org`` and no stored cloud id: ``POST /api/sync/projects`` with
    the project's tag and name. ``409 tag_taken`` stops, stores no mapping, and
    returns ``owner_label``. Any other answer that is not a created or adopted
    project also stores nothing and seeds nothing.

    The return always carries ``backfill_status`` plus ``owner_label`` (None
    unless the tag is taken), ``reason`` (None on a seed), ``seeded`` and
    ``skipped``.
    """
    moment = _now(now)
    project = db.get(Project, project_id)
    if project is None:
        status = backfill_status(db, project_id)
        return {**status, "owner_label": None, "reason": "absent", "seeded": 0, "skipped": 0}

    mapping = db.get(SyncProject, project_id)
    if create_in_org and not _cloud_id(mapping):
        result = register_project(
            db, tag=project.tag, name=project.name, transport=transport,
        )
        if result.status == "refused" and result.reason == "tag_taken":
            # Stop. The tag names someone else's project. Writing a mapping, or
            # sending under a renamed tag, would break that identity.
            status = backfill_status(db, project_id)
            return {
                **status, "state": "refused", "owner_label": result.owner_label,
                "reason": "tag_taken", "seeded": 0, "skipped": 0,
            }
        if result.status != "ok" or not result.cloud_project_id:
            status = backfill_status(db, project_id)
            # A definite no stays refused. No answer is not a no: the entities
            # are simply not confirmed, which ``backfill_status`` already says.
            state = "refused" if result.status == "refused" else status["state"]
            return {
                **status, "state": state, "owner_label": None,
                "reason": result.reason, "seeded": 0, "skipped": 0,
            }
        _store_cloud(
            db, project, cloud_project_id=result.cloud_project_id,
            tag=result.tag or project.tag, now=moment,
        )

    records = list_entities(db, project_id) or []
    seeded, skipped = _seed(db, project_id, records, moment)
    db.commit()
    status = backfill_status(db, project_id)
    return {**status, "owner_label": None, "reason": None, "seeded": seeded, "skipped": skipped}
