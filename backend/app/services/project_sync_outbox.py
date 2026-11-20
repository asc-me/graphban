"""Box-side outbox of entities a fully synced project has changed (GRPH-1733, P89).

Capture writes here and nowhere else. There is no network I/O in this module: the flush
that drains `due()` is a later slice. A project at `digests`, and a project with no
`sync_project` row, write nothing.

Repeated changes to one entity update the same row. `version` counts the changes,
`dirty_at` moves, `first_dirty_at` stays. The primary key is what makes a second row
impossible, and that is what holds the outbox inside `CAP_BYTES` (D11): the table cannot
grow by repetition.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.models import SyncOutbox, SyncProject

logger = logging.getLogger("graphban.project_sync_outbox")

#: Today's behaviour. Existing mappings, and any new mapping that does not opt in, stay here.
SYNC_LEVEL_DIGESTS = "digests"
#: Full replica. The only level that captures.
SYNC_LEVEL_SYNC = "sync"
SYNC_LEVELS = (SYNC_LEVEL_DIGESTS, SYNC_LEVEL_SYNC)

#: D4. `comments` is the default the migration stamps on rows that predate the column.
CLOUD_PERMISSION_READ_ONLY = "read_only"
CLOUD_PERMISSION_COMMENTS = "comments"
CLOUD_PERMISSION_PROPOSALS = "proposals"
CLOUD_PERMISSIONS = (
    CLOUD_PERMISSION_READ_ONLY, CLOUD_PERMISSION_COMMENTS, CLOUD_PERMISSION_PROPOSALS,
)

#: Flush order. Lower is sooner. `due()` orders by this, then by who has been waiting longest.
CHANGE_STATUS = "status"
CHANGE_NEW = "new"
CHANGE_EDIT = "edit"
CHANGE_HISTORY = "history"
CHANGE_CLASSES = (CHANGE_STATUS, CHANGE_NEW, CHANGE_EDIT, CHANGE_HISTORY)
_RANK = {name: i for i, name in enumerate(CHANGE_CLASSES)}

#: D11. One row per entity is what enforces it; `backlog_bytes` is the estimate of the rows
#: actually stored, so a later screen can say `backlog full` rather than report zero for a
#: table nobody measured.
CAP_BYTES = 100 * 1024 * 1024

_STATUS_ACTIONS = frozenset({"claim_next", "release_item", "sign_off"})
_ENTITY_TYPES = {
    "item": "item",
    "prd": "prd",
    "comment": "comment",
    "request_comment": "comment",
}


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _is_history(action: str, meta: dict) -> bool:
    if "evidence" in meta or "history" in meta:
        return True
    lowered = action.lower()
    return "evidence" in lowered or "history" in lowered


def classify(target_type: str, target_id: str, action: str,
             meta: dict | None) -> tuple[str, str, str] | None:
    """The entity a recorded change dirties, and which flush class it is.

    `None` means this event is not a synced change (a refusal, a heartbeat, a credential,
    anything with no entity). A create is `new` even when the payload also carries a status:
    the entity has not been sent yet. A later status change on the same row outranks it.
    """
    action = action or ""
    meta = meta if isinstance(meta, dict) else {}
    if "refus" in action or action == "heartbeat":
        return None
    if action == "request_comment" and meta.get("comment_id"):
        return "comment", str(meta["comment_id"]), CHANGE_NEW
    entity_type = _ENTITY_TYPES.get(target_type or "")
    if entity_type is None or not target_id:
        return None
    entity_id = str(target_id)
    if action.startswith("create"):
        return entity_type, entity_id, CHANGE_NEW
    if (action.startswith("blocker.") or action in _STATUS_ACTIONS
            or "status" in meta or "blocker" in meta):
        return entity_type, entity_id, CHANGE_STATUS
    if _is_history(action, meta):
        return entity_type, entity_id, CHANGE_HISTORY
    return entity_type, entity_id, CHANGE_EDIT


def _keep_class(current: str, incoming: str) -> str:
    """The better of the two pending classes. A worse later change must not hide a better one."""
    if current not in _RANK:
        return incoming if incoming in _RANK else CHANGE_EDIT
    if incoming not in _RANK:
        return current
    return current if _RANK[current] <= _RANK[incoming] else incoming


def _content_hash(entity_type: str, entity_id: str, action: str, meta: dict | None) -> str:
    payload = json.dumps(
        {"entity_type": entity_type, "entity_id": entity_id, "action": action,
         "meta": meta or {}},
        sort_keys=True, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _pending(db: Session, project_id: str, entity_type: str, entity_id: str) -> SyncOutbox | None:
    """The row already in this transaction, or the one committed earlier.

    `Session.get` does not see an instance added during the current flush and not yet
    expired into the identity map the way a same-flush second change needs, so the pending
    list is checked first.
    """
    for obj in db.new:
        if (isinstance(obj, SyncOutbox) and obj.project_id == project_id
                and obj.entity_type == entity_type and obj.entity_id == entity_id):
            return obj
    return db.get(SyncOutbox, (project_id, entity_type, entity_id))


def capture(db: Session, *, project_id: str, entity_type: str, entity_id: str,
            change_class: str, action: str = "", meta: dict | None = None,
            now: datetime | None = None) -> SyncOutbox | None:
    """Upsert the one outbox row for this entity. Does not commit.

    Returns `None` when the project is not at `sync` — including when it has no
    `sync_project` row at all. A parked row stays parked: a new change still advances the
    version, and unparking is a later slice.
    """
    if not project_id or not entity_type or not entity_id:
        return None
    mapping = db.get(SyncProject, project_id)
    if mapping is None or mapping.sync_level != SYNC_LEVEL_SYNC:
        return None
    if change_class not in _RANK:
        change_class = CHANGE_EDIT
    moment = _now(now)
    digest = _content_hash(entity_type, entity_id, action, meta)
    row = _pending(db, project_id, entity_type, entity_id)
    if row is None:
        row = SyncOutbox(
            project_id=project_id, entity_type=entity_type, entity_id=entity_id,
            version=1, hash=digest, dirty_at=moment, first_dirty_at=moment,
            attempts=0, change_class=change_class,
        )
        db.add(row)
        return row
    row.version = (row.version or 0) + 1
    row.hash = digest
    row.dirty_at = moment
    row.change_class = _keep_class(row.change_class, change_class)
    return row


def note_event(db: Session, *, project_id: str | None, target_type: str = "",
               target_id: str = "", action: str = "", meta: dict | None = None,
               now: datetime | None = None) -> SyncOutbox | None:
    """Capture one recorded change. A failure here does not fail the audit that called it.

    The caller flushes its own event first. The savepoint then rolls back only the outbox
    write, so a broken capture cannot take the audit row with it.
    """
    classified = classify(target_type, target_id, action, meta)
    if classified is None or not project_id:
        return None
    entity_type, entity_id, change_class = classified
    try:
        with db.begin_nested():
            return capture(
                db, project_id=project_id, entity_type=entity_type, entity_id=entity_id,
                change_class=change_class, action=action, meta=meta, now=now,
            )
    except Exception:  # noqa: BLE001 — capture must not break the write it observes
        logger.exception("sync outbox capture failed for %s %s", target_type, target_id)
        return None


def due(db: Session, now: datetime, debounce: int = 5, max_delay: int = 60) -> list[SyncOutbox]:
    """Rows ready to flush at `now`, unparked only.

    A row is due when `dirty_at` is at least `debounce` seconds old, or when
    `first_dirty_at` is at least `max_delay` seconds old (a busy entity whose last change
    keeps refreshing `dirty_at` still goes out). Order: status and blocker changes, then
    new entities, then edits, then history and evidence; within a class, the one waiting
    longest first.
    """
    moment = _now(now)
    quiet = moment - timedelta(seconds=debounce)
    waiting = moment - timedelta(seconds=max_delay)
    rank = case(
        (SyncOutbox.change_class == CHANGE_STATUS, 0),
        (SyncOutbox.change_class == CHANGE_NEW, 1),
        (SyncOutbox.change_class == CHANGE_EDIT, 2),
        else_=3,
    )
    rows = db.scalars(
        select(SyncOutbox)
        .where(or_(SyncOutbox.parked_reason.is_(None), SyncOutbox.parked_reason == ""))
        .where(or_(SyncOutbox.dirty_at <= quiet, SyncOutbox.first_dirty_at <= waiting))
        .order_by(rank, SyncOutbox.first_dirty_at, SyncOutbox.entity_type, SyncOutbox.entity_id)
    ).all()
    return list(rows)


def _row_bytes(row: SyncOutbox) -> int:
    """A stored-row estimate: the text this row actually holds, plus the fixed columns."""
    parts = (
        row.project_id, row.entity_type, row.entity_id, row.hash or "",
        row.parked_reason or "", row.parked_contract or "", row.change_class or "",
    )
    return 64 + sum(len(part.encode()) for part in parts)


def backlog_bytes(db: Session) -> int:
    """Bytes the stored rows estimate to. Zero means the table was counted and is empty."""
    return sum(_row_bytes(row) for row in db.scalars(select(SyncOutbox)).all())


def project_queue(db: Session, project_id: str) -> dict:
    """Queued count, parked count and reasons, and backlog bytes for one project.

    An empty ``project_id`` matches nothing and returns zeros — the table was not
    scanned. Zeros on a real id mean that project was read and has no rows.
    """
    if not project_id:
        return {"queued": 0, "parked": 0, "reasons": [], "backlog_bytes": 0}
    rows = list(db.scalars(
        select(SyncOutbox).where(SyncOutbox.project_id == project_id)
    ).all())
    queued = parked = 0
    reasons: list[str] = []
    total = 0
    for row in rows:
        total += _row_bytes(row)
        reason = (row.parked_reason or "").strip()
        if reason:
            parked += 1
            if reason not in reasons:
                reasons.append(reason)
        else:
            queued += 1
    reasons.sort()
    return {"queued": queued, "parked": parked, "reasons": reasons, "backlog_bytes": total}
