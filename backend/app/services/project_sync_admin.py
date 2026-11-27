"""Box-admin surface for per-project sync (GRPH-1739, P89 S7).

The router is the gate. This module reads the outbox and calls the services
from S1–S6. It does not decide who is an admin.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, SyncProject, User
from app.services import project_sync_backfill as backfill
from app.services import project_sync_flush as flush
from app.services import project_sync_lifecycle as lifecycle
from app.services import project_sync_outbox as outbox


class TagTaken(Exception):
    """``409 tag_taken``. The owner label is what the wizard shows."""

    def __init__(self, owner_label: str):
        self.owner_label = owner_label
        super().__init__(owner_label)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _held(project_id: str, queued: int, reasons: list[str]) -> str | None:
    """``budget``, ``retry_after``, ``contract``, or ``None``.

    A box-wide wait applies to a project only when that project still has
    something unparked to send. A contract park is this project's own rows.
    Anything else parked (too large, invalid) is a reason on ``parked``, not
    a hold of the queue.
    """
    box = flush.box_hold(_now())
    if box == "retry_after" and queued:
        return "retry_after"
    if box == "budget" and queued:
        return "budget"
    if "contract" in reasons:
        return "contract"
    return None


def _push_result(mapping: SyncProject | None) -> str | None:
    """The stored result, unchanged. ``unknown`` stays ``unknown`` — it is not
    rewritten into a success, and a project that has never pushed stays null."""
    if mapping is None:
        return None
    result = mapping.last_push_result
    if result is None or result == "":
        return None
    return result


def _reported_backfill(db: Session, project_id: str, level: str) -> dict:
    """Full-sync progress only while the project is at ``sync``.

    Local only and digests do not push. Leaving sync clears the manifest, and
    ``backfill_status`` then counts every current entity as unconfirmed:
    ``running``, confirmed 0 of N, which cannot move. ``off`` says this project
    is not in a full-sync backfill. It is not an unread field. Returning
    ``backfill_status`` here for a local project fails
    ``test_local_with_purge_reports_backfill_off_not_running``.
    """
    if level != outbox.SYNC_LEVEL_SYNC:
        return {"state": "off"}
    return backfill.backfill_status(db, project_id)


def project_view(db: Session, project_id: str) -> dict:
    """One local project's sync state. A missing row is local only, not digests.

    ``backlog_full`` is the whole outbox against ``CAP_BYTES`` (D11), not this
    project's own rows. An empty ``project_id`` was not scanned, so the flag is
    null rather than false. ``backfill`` is ``{state: off}`` unless the level
    is ``sync``.
    """
    mapping = db.get(SyncProject, project_id)
    queue = outbox.project_queue(db, project_id)
    level = mapping.sync_level if mapping is not None else lifecycle.LEVEL_LOCAL
    permission = mapping.cloud_permission if mapping is not None else None
    when = mapping.last_push_at if mapping is not None else None
    # The cap is the table. This project's `backlog_bytes` can sit under it while
    # another project's rows put the outbox over. An empty id did not scan.
    if project_id:
        full = outbox.backlog_full(outbox.backlog_bytes(db))
    else:
        full = None
    return {
        "project_id": project_id,
        "sync_level": level,
        "cloud_permission": permission,
        "backfill": _reported_backfill(db, project_id, level),
        "queued": queue["queued"],
        "parked": {"count": queue["parked"], "reasons": queue["reasons"]},
        "backlog_bytes": queue["backlog_bytes"],
        "backlog_full": full,
        "held_by": _held(project_id, queue["queued"], queue["reasons"]),
        "last_push_at": when,
        "last_push_result": _push_result(mapping),
        # GRPH-1978: the console's choice, the box's own level and what goes out. The console
        # narrows the box's level; `effective_sync` is the one reported and obeyed.
        "console_sync": mapping.console_sync if mapping is not None else None,
        "console_home": mapping.console_home if mapping is not None else None,
        "effective_sync": lifecycle.effective_sync(mapping) if mapping is not None else None,
        "reported_sync": mapping.reported_sync if mapping is not None else None,
    }


def local_projects(db: Session) -> list[dict]:
    """Every local project, mapped or not. An empty list means there are no
    projects, not that the read was skipped."""
    ids = list(db.scalars(select(Project.id).order_by(Project.id)).all())
    return [project_view(db, project_id) for project_id in ids]


def _backfill_body(started: dict | None) -> dict | None:
    if started is None:
        return None
    return {
        "total": started.get("total", 0),
        "confirmed": started.get("confirmed", 0),
        "parked": started.get("parked", 0),
        "state": started.get("state"),
        "seeded": started.get("seeded", 0),
        "skipped": started.get("skipped", 0),
        "reason": started.get("reason"),
        "owner_label": started.get("owner_label"),
    }


def _raise_if_refused(started: dict) -> None:
    if started.get("reason") == "tag_taken":
        raise TagTaken(started.get("owner_label") or "")
    # A definite no that seeded nothing is the console's refusal, not a sync
    # that is quietly running. No answer (reason set, state still running)
    # is not a no: the caller reports the status the backfill already computed.
    if (started.get("reason") and started.get("state") == "refused"
            and not started.get("seeded")):
        raise ValueError(started["reason"])


def _point(db: Session, project_id: str, cloud_project_id: str, tag: str) -> None:
    """Map this one project to ``cloud_project_id``. No other row is touched.

    A new row starts at ``digests`` (the column default), so the level change
    that follows is the one that turns sync on. Re-pointing is refused while
    the project syncs or owes a purge: its manifest describes the old replica.
    """
    row = db.get(SyncProject, project_id)
    if row is None:
        db.add(SyncProject(local_project_id=project_id, cloud_project_id=cloud_project_id,
                           cloud_project_tag=tag, sync_level=outbox.SYNC_LEVEL_DIGESTS))
        db.commit()
        return
    if (row.cloud_project_id or "").strip() == cloud_project_id:
        if tag:
            row.cloud_project_tag = tag
            db.commit()
        return
    if row.sync_level == outbox.SYNC_LEVEL_SYNC or (row.pending_purge or ""):
        raise ValueError(
            "this project syncs to another cloud project — set it to local only first")
    row.cloud_project_id = cloud_project_id
    row.cloud_project_tag = tag
    row.last_push_at = row.last_push_result = row.last_push_reason = None
    # The console's choice and the level it stored belong to the old cloud project.
    row.console_sync = row.console_home = row.reported_sync = None
    db.commit()


def configure_project(db: Session, project_id: str, *, sync_level: str | None,
                      cloud_permission: str | None, create_in_org: bool,
                      purge: bool, user: User, cloud_project_id: str | None = None,
                      cloud_project_tag: str = "") -> dict:
    """Apply one project's level, create-in-org or cloud target, and permission.

    ``tag_taken`` raises ``TagTaken`` and stores no mapping. A project that is
    already mapped keeps its mapping: create-in-org then syncs to the cloud
    project it has, and a refusal changes nothing. The caller turns
    ``TagTaken`` into 409. A missing project is ``LookupError``.
    """
    if db.get(Project, project_id) is None:
        raise LookupError(project_id)
    target = (cloud_project_id or "").strip()
    if sync_level is None and cloud_permission is None and not create_in_org and not target:
        raise ValueError("nothing to change")
    if target and create_in_org:
        raise ValueError("cloud_project_id and create_in_org are exclusive")
    if target and sync_level not in (outbox.SYNC_LEVEL_SYNC, outbox.SYNC_LEVEL_DIGESTS):
        raise ValueError("cloud_project_id needs sync_level sync or digests")
    if target:
        _point(db, project_id, target, (cloud_project_tag or "").strip())

    started: dict | None = None
    if sync_level == lifecycle.LEVEL_LOCAL:
        lifecycle.set_level(
            db, project_id, lifecycle.LEVEL_LOCAL, purge=purge, user=user)
    elif sync_level == outbox.SYNC_LEVEL_DIGESTS:
        lifecycle.set_level(
            db, project_id, outbox.SYNC_LEVEL_DIGESTS, purge=purge, user=user)
    elif sync_level == outbox.SYNC_LEVEL_SYNC or create_in_org:
        mapping = db.get(SyncProject, project_id)
        has_cloud = bool(mapping and (mapping.cloud_project_id or "").strip())
        if create_in_org and not has_cloud:
            started = backfill.start_backfill(db, project_id, True)
            _raise_if_refused(started)
        elif has_cloud:
            lifecycle.set_level(
                db, project_id, outbox.SYNC_LEVEL_SYNC, purge=False, user=user)
            started = backfill.start_backfill(db, project_id, False)
            _raise_if_refused(started)
        else:
            raise ValueError("a local-only project has no cloud project to sync")
    elif sync_level is not None:
        raise ValueError(
            f"invalid sync level: {sync_level!r} — one of "
            f"{outbox.SYNC_LEVEL_SYNC}, {outbox.SYNC_LEVEL_DIGESTS}, {lifecycle.LEVEL_LOCAL}")

    if cloud_permission is not None:
        lifecycle.set_cloud_permission(db, project_id, cloud_permission, user=user)

    return {"project": project_view(db, project_id), "backfill": _backfill_body(started)}
