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


def project_view(db: Session, project_id: str) -> dict:
    """One local project's sync state. A missing row is local only, not digests."""
    mapping = db.get(SyncProject, project_id)
    queue = outbox.project_queue(db, project_id)
    level = mapping.sync_level if mapping is not None else lifecycle.LEVEL_LOCAL
    permission = mapping.cloud_permission if mapping is not None else None
    when = mapping.last_push_at if mapping is not None else None
    return {
        "project_id": project_id,
        "sync_level": level,
        "cloud_permission": permission,
        "backfill": backfill.backfill_status(db, project_id),
        "queued": queue["queued"],
        "parked": {"count": queue["parked"], "reasons": queue["reasons"]},
        "backlog_bytes": queue["backlog_bytes"],
        "held_by": _held(project_id, queue["queued"], queue["reasons"]),
        "last_push_at": when,
        "last_push_result": _push_result(mapping),
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


def configure_project(db: Session, project_id: str, *, sync_level: str | None,
                      cloud_permission: str | None, create_in_org: bool,
                      purge: bool, user: User) -> dict:
    """Apply one project's level, create-in-org, and permission.

    ``tag_taken`` raises ``TagTaken`` and stores no mapping. The caller turns
    that into 409. A missing project is ``LookupError``.
    """
    if db.get(Project, project_id) is None:
        raise LookupError(project_id)
    if sync_level is None and cloud_permission is None and not create_in_org:
        raise ValueError("nothing to change")

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
