"""Drain the project-sync outbox to the console (GRPH-1735, P89 D6, D9, D11).

One pass reads the contract once, then sends due rows (GRPH-1733 ``due``) built by the
payload serializer (GRPH-1734) through the client (GRPH-1732). The body is gzipped by the
client. This module counts those compressed bytes against the per-box budget.

A ``200`` writes ``SyncManifest`` for each stored entity and deletes the outbox row only
when its version is still the one that was read. A change during the send stays dirty.
A per-entity refusal parks that entity and does not stop the rest of the batch. ``429``,
a timeout, and ``5xx`` leave the rows dirty and unparked, and nothing is sent again
before ``Retry-After`` or the jittered backoff, whichever is later.
"""
from __future__ import annotations

import gzip
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SyncManifest, SyncOutbox, SyncProject
from app.services.project_sync_client import CONTRACT, DEFAULT_MAX_DELAY, get_contract, push_entities
from app.services.project_sync_outbox import SYNC_LEVEL_SYNC, due
from app.services.project_sync_payload import entity as payload_entity

logger = logging.getLogger("graphban.project_sync_flush")

#: D11. A batch stops at whichever ceiling it hits first.
BATCH_ENTITIES = 200
BATCH_BYTES = 1024 * 1024
_WINDOW = timedelta(seconds=60)
_BACKSTOP = timedelta(hours=4)
_BACKOFF_CAP = 15 * 60


@dataclass
class FlushState:
    """Process-local budget and backoff. Tests pass their own; the loop keeps one."""

    not_before: datetime | None = None
    failures: int = 0
    sent: list[tuple[datetime, int]] = field(default_factory=list)


_PROCESS = FlushState()


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _byte_limit() -> int:
    return max(0, int(settings.sync_flush_bytes_per_min))


def _request_limit() -> int:
    return max(0, int(settings.sync_flush_requests_per_min))


def _prune(state: FlushState, now: datetime) -> None:
    cutoff = now - _WINDOW
    state.sent = [(at, size) for at, size in state.sent if at > cutoff]


def _bytes_used(state: FlushState, now: datetime) -> int:
    _prune(state, now)
    return sum(size for _, size in state.sent)


def _requests_used(state: FlushState, now: datetime) -> int:
    _prune(state, now)
    return len(state.sent)


def gzip_size(entities: list[dict]) -> int:
    """Compressed size of the body ``push_entities`` will send.

    The JSON matches the client's: sorted keys, no extra whitespace. The gzip header
    is a fixed width, so the length does not depend on the clock.
    """
    raw = json.dumps(
        {"contract": CONTRACT, "entities": list(entities)},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    return len(gzip.compress(raw))


def _backoff_seconds(failures: int, retry_after: int | None, rng) -> float:
    """Jittered exponential backoff, never sooner than the console's ``Retry-After``.

    ``failures`` is how many consecutive failures already happened, so the first wait
    uses a base of one second. ``rng`` returns a float in ``[0, 1)``.
    """
    base = min(_BACKOFF_CAP, 2 ** min(failures, 10))
    jittered = base * (0.5 + 0.5 * float(rng()))
    return max(float(retry_after or 0), jittered)


def _backoff(state: FlushState, now: datetime, retry_after: int | None, rng) -> None:
    wait = _backoff_seconds(state.failures, retry_after, rng)
    state.failures += 1
    state.not_before = now + timedelta(seconds=wait)


def _parked(db: Session) -> list[SyncOutbox]:
    return list(db.scalars(
        select(SyncOutbox).where(
            SyncOutbox.parked_reason.is_not(None),
            SyncOutbox.parked_reason != "",
        )
    ).all())


def _park(rows: list[SyncOutbox], *, reason: str, contract: str | None) -> None:
    for row in rows:
        row.parked_reason = reason
        row.parked_contract = contract


def _requeue(db: Session, advertised: str, now: datetime) -> None:
    """Clear parks whose contract is no longer the one the console advertises, and
    parks that have sat for the 4h backstop. A plain entity refusal has no
    ``parked_contract`` and waits for the backstop or ``retry_parked``.
    """
    horizon = now - _BACKSTOP
    for row in _parked(db):
        parked_at = _aware(row.first_dirty_at)
        version_moved = bool(row.parked_contract) and row.parked_contract != advertised
        if version_moved or parked_at <= horizon:
            row.parked_reason = None
            row.parked_contract = None


def retry_parked(db: Session, project_id: str) -> int:
    """Put this project's parked rows back on the queue. The Retry now button.

    Returns how many rows were parked. Zero means this project had none — the
    table was read. An empty ``project_id`` matches nothing.
    """
    if not project_id:
        return 0
    rows = list(db.scalars(
        select(SyncOutbox).where(
            SyncOutbox.project_id == project_id,
            SyncOutbox.parked_reason.is_not(None),
            SyncOutbox.parked_reason != "",
        )
    ).all())
    for row in rows:
        row.parked_reason = None
        row.parked_contract = None
    db.commit()
    return len(rows)


def _by_project(rows: list[SyncOutbox]) -> list[tuple[str, list[SyncOutbox]]]:
    order: list[str] = []
    groups: dict[str, list[SyncOutbox]] = {}
    for row in rows:
        if row.project_id not in groups:
            order.append(row.project_id)
            groups[row.project_id] = []
        groups[row.project_id].append(row)
    return [(pid, groups[pid]) for pid in order]


def _mark(mapping: SyncProject | None, *, result: str, reason: str | None, now: datetime) -> None:
    if mapping is None:
        return
    mapping.last_push_at = now
    mapping.last_push_result = result
    mapping.last_push_reason = reason


def _confirm(db: Session, row: SyncOutbox, record: dict, now: datetime) -> None:
    key = (row.project_id, row.entity_type, row.entity_id)
    manifest = db.get(SyncManifest, key)
    if manifest is None:
        manifest = SyncManifest(
            project_id=row.project_id, entity_type=row.entity_type, entity_id=row.entity_id,
        )
        db.add(manifest)
    manifest.version = record["version"]
    manifest.hash = record["hash"]
    manifest.confirmed_at = now


def _afford(ready: list[tuple], state: FlushState, now: datetime) -> list[tuple]:
    """The prefix that fits this minute and the batch cap. Empty means send nothing."""
    if not ready or _requests_used(state, now) >= _request_limit():
        return []
    room = _byte_limit() - _bytes_used(state, now)
    if room <= 0:
        return []
    chosen: list[tuple] = []
    for item in ready:
        trial = chosen + [item]
        if len(trial) > BATCH_ENTITIES:
            break
        size = gzip_size([record for _, _, record in trial])
        if size > room or size > BATCH_BYTES:
            break
        chosen = trial
    if chosen:
        return chosen
    # One entity that cannot fit a normal batch still has to be offered, or it
    # sits at the head of the queue forever. It still has to fit the minute.
    size = gzip_size([ready[0][2]])
    if size > BATCH_BYTES and size <= room:
        return [ready[0]]
    return []


def _note_attempts(rows: list[tuple]) -> None:
    for row, _, _ in rows:
        row.attempts = (row.attempts or 0) + 1


def flush_once(db: Session, *, now: datetime, transport=None, state: FlushState | None = None,
               rng=None) -> int:
    """Send what is due at ``now``. Returns how many batches were accepted.

    ``state`` carries the budget and the next time a send is allowed. The loop
    omits it and uses the process state. ``rng`` supplies jitter; the default
    is ``random.random``.
    """
    state = _PROCESS if state is None else state
    rng = random.random if rng is None else rng
    now = _aware(now)
    # The console's Retry-After is a floor on every later call, including the
    # contract read. Deleting this check sends during the wait and fails
    # test_a_429_retry_after_sends_nothing_until_the_wait_is_over.
    if state.not_before is not None and now < state.not_before:
        return 0

    owed = due(db, now)
    if not owed and not _parked(db):
        return 0

    contract = get_contract(db, transport=transport)
    if contract.status == "ok" and contract.version == CONTRACT:
        advertised = contract.version
    elif contract.status == "refused" and contract.reason == "contract":
        advertised = contract.version or ""
        _requeue(db, advertised, now)
        db.flush()
        _park(due(db, now), reason="contract", contract=advertised or None)
        for pid, _rows in _by_project(owed):
            _mark(db.get(SyncProject, pid), result="refused", reason="contract", now=now)
        db.commit()
        return 0
    else:
        _backoff(state, now, contract.retry_after, rng)
        for pid, _rows in _by_project(owed):
            _mark(db.get(SyncProject, pid), result="unknown", reason=contract.reason, now=now)
        db.commit()
        return 0

    _requeue(db, advertised, now)
    db.flush()
    sent = 0
    for project_id, group in _by_project(due(db, now)):
        mapping = db.get(SyncProject, project_id)
        if mapping is None or mapping.sync_level != SYNC_LEVEL_SYNC or not mapping.cloud_project_id:
            continue
        ready: list[tuple] = []
        for row in group:
            seen = row.version
            record = payload_entity(db, row.entity_type, row.entity_id)
            if record is None:
                continue
            manifest = db.get(SyncManifest, (row.project_id, row.entity_type, row.entity_id))
            if manifest is not None and manifest.hash == record["hash"]:
                # The console already has this body. Sending it would be a no-op.
                # A version that moved while we looked is a newer edit, so the row stays.
                if row.version == seen:
                    db.delete(row)
                continue
            ready.append((row, seen, record))
        while ready:
            fitted = _afford(ready, state, now)
            if not fitted:
                db.commit()
                return sent
            entities = [record for _, _, record in fitted]
            result = push_entities(
                db, cloud_project_id=mapping.cloud_project_id, entities=entities,
                max_delay=DEFAULT_MAX_DELAY, transport=transport,
            )
            size = gzip_size(entities)
            state.sent.append((now, size))
            if result.status == "unknown":
                _note_attempts(fitted)
                _mark(mapping, result="unknown", reason=result.reason, now=now)
                _backoff(state, now, result.retry_after, rng)
                db.commit()
                return sent
            if result.status == "refused" and result.reason == "contract":
                version = result.version or advertised
                _park([row for row, _, _ in fitted] + [row for row, _, _ in ready[len(fitted):]],
                      reason="contract", contract=version)
                _mark(mapping, result="refused", reason="contract", now=now)
                db.commit()
                return sent
            if result.status != "ok":
                _park([row for row, _, _ in fitted], reason=result.reason or "refused", contract=None)
                _mark(mapping, result="refused", reason=result.reason, now=now)
                db.commit()
                return sent
            stored = {(row["type"], row["id"]) for row in result.stored}
            refused = {
                (row["type"], row["id"]): (row.get("reason") or "refused")
                for row in result.refused
            }
            for outbox_row, seen, record in fitted:
                key = (record["type"], record["id"])
                if key in refused and key not in stored:
                    outbox_row.parked_reason = refused[key]
                    outbox_row.parked_contract = None
                    continue
                if key not in stored:
                    continue
                _confirm(db, outbox_row, record, now)
                # Read the version we captured, not the attribute after a concurrent edit.
                # The row stays dirty when that version moved during the send.
                if outbox_row.version == seen:
                    db.delete(outbox_row)
            state.failures = 0
            state.not_before = None
            _mark(mapping, result="ok", reason=None, now=now)
            sent += 1
            ready = ready[len(fitted):]
    db.commit()
    return sent
