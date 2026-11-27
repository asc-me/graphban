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

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SyncManifest, SyncOutbox, SyncProject
from app.services.project_sync_client import CONTRACT, DEFAULT_MAX_DELAY, get_contract, push_entities
from app.services.project_sync_outbox import SYNC_LEVEL_SYNC, due, version_in_range
from app.services.project_sync_payload import entity as payload_entity
from app.services import project_sync_lifecycle as lifecycle

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

#: Admin override of the per-box budget (GRPH-1739). ``None`` means the env default.
#: Process-local, same as ``_PROCESS``: the flush loop and ``GET /api/sync/budget``
#: share this process.
_BUDGET: dict[str, int | None] = {"bytes": None, "requests": None}


def reset_runtime() -> None:
    """Drop the process budget override and the hold. Tests call this so one
    case cannot leave the next one looking held or over a budget nobody set."""
    _BUDGET["bytes"] = None
    _BUDGET["requests"] = None
    _PROCESS.not_before = None
    _PROCESS.failures = 0
    _PROCESS.sent.clear()


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _byte_limit() -> int:
    raw = _BUDGET["bytes"]
    if raw is None:
        raw = settings.sync_flush_bytes_per_min
    return max(0, int(raw))


def _request_limit() -> int:
    raw = _BUDGET["requests"]
    if raw is None:
        raw = settings.sync_flush_requests_per_min
    return max(0, int(raw))


def budget_view() -> dict:
    """The live per-box budget. An override wins over the env default."""
    return {"bytes_per_min": _byte_limit(), "requests_per_min": _request_limit()}


def set_budget(bytes_per_min: int, requests_per_min: int) -> dict:
    """Replace the live budget. Negative is refused. The env default is what
    ``reset_runtime`` returns to, not what a zero means — zero is a real ceiling."""
    if bytes_per_min < 0 or requests_per_min < 0:
        raise ValueError("budget limits cannot be negative")
    _BUDGET["bytes"] = int(bytes_per_min)
    _BUDGET["requests"] = int(requests_per_min)
    return budget_view()


def box_hold(now: datetime) -> str | None:
    """Why the box is not sending, or ``None`` when it is free to send.

    ``retry_after`` is a wait still in the future (a 429, a timeout, a 5xx).
    ``budget`` is this minute's ceiling already spent, or a ceiling of zero.
    A contract park is per project and is not decided here.
    """
    now = _aware(now)
    state = _PROCESS
    if state.not_before is not None and now < _aware(state.not_before):
        return "retry_after"
    used_bytes = _bytes_used(state, now)
    used_requests = _requests_used(state, now)
    byte_limit = _byte_limit()
    request_limit = _request_limit()
    spent = used_requests >= request_limit or used_bytes >= byte_limit
    if not spent:
        return None
    if used_bytes or used_requests or byte_limit == 0 or request_limit == 0:
        return "budget"
    return None


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


def _park(rows: list[SyncOutbox], *, reason: str, contract: str | None, now: datetime) -> None:
    """Park ``rows`` and restart their wait.

    ``first_dirty_at`` is the park time while a row is parked: the 4h backstop counts
    from it, and every re-park moves it, so a row the console keeps refusing waits
    another 4h each time instead of going out on every pass once the first stamp is old.
    """
    for row in rows:
        row.parked_reason = reason
        row.parked_contract = contract
        row.first_dirty_at = now


def _delete_if_unchanged(db: Session, row: SyncOutbox, seen: int) -> bool:
    """Delete the outbox row only if the STORED version is still ``seen``.

    The check is in the DELETE itself, not on the in-memory attribute: another session
    (or a statement this session's identity map never saw) may have bumped the version
    during the send, and that newer change has to stay queued. Returns whether it went.
    """
    table = SyncOutbox.__table__
    gone = db.execute(
        delete(table).where(
            table.c.project_id == row.project_id,
            table.c.entity_type == row.entity_type,
            table.c.entity_id == row.entity_id,
            table.c.version == seen,
        )
    ).rowcount
    if gone:
        db.expunge(row)
    else:
        db.expire(row)
    return bool(gone)


def _old_invalid(row: SyncOutbox) -> bool:
    """A row the old hash version parked as ``invalid``.

    D6 unparks a contract park when the advertised contract moves. An ``invalid``
    refusal stored no contract, so those rows would sit until the 4h backstop
    and the replica would stay empty after this upgrade. A refusal parked by
    this build stamps the contract, so this does not put it straight back.
    """
    return (row.parked_reason or "").strip() == "invalid" and not (row.parked_contract or "").strip()


def _requeue(db: Session, advertised: str, now: datetime) -> None:
    """Clear parks whose contract is no longer the one the console advertises,
    parks that have sat for the 4h backstop, and ``invalid`` parks left by the
    hash version. A plain entity refusal from this build has a contract (or
    waits for the backstop / ``retry_parked``).
    """
    horizon = now - _BACKSTOP
    for row in _parked(db):
        parked_at = _aware(row.first_dirty_at)  # the park time; see _park
        version_moved = bool(row.parked_contract) and row.parked_contract != advertised
        if version_moved or parked_at <= horizon or _old_invalid(row):
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
    """The prefix that fits this minute and the batch cap. Empty means send nothing.

    Every entity reaching here fits an empty minute on its own (oversized ones are
    parked first), so an empty prefix only means this minute's budget is spent.
    """
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
    return chosen


def _ceiling() -> int:
    """The largest body any single request can carry: an empty minute or the batch cap."""
    return min(_byte_limit(), BATCH_BYTES)


def _note_attempts(rows: list[tuple]) -> None:
    for row, _, _ in rows:
        row.attempts = (row.attempts or 0) + 1


#: A 422 naming one of these is the console's stored level refusing holders or a non-home
#: item (GBC-P23). It is not the entity's fault, so the rows are not parked for it.
_LEVEL_FIELDS = ("assignee", "held_by", "entities")


def _rebuild(db: Session, fitted: list[tuple], mapping: SyncProject | None) -> list[tuple]:
    """``fitted`` again, each body rebuilt under the level the console now holds. A
    non-home box that fell to ``summaries`` sends none of them."""
    if mapping is None or (lifecycle.non_home(mapping) and not lifecycle.holders_on(mapping)):
        return []
    out = []
    for row, seen, _old in fitted:
        if lifecycle.non_home(mapping) and row.entity_type != "item":
            continue
        record = payload_entity(db, row.entity_type, row.entity_id)
        if record is not None:
            out.append((row, seen, record))
    return out


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
        _park(due(db, now), reason="contract", contract=advertised or None, now=now)
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
        # GBC-P23: the console stores this box's effective level before any body that
        # carries holders, or any non-home item, is sent. Posts only when it moved.
        lifecycle.report(db, mapping, transport=transport, now=now)
        mapping = db.get(SyncProject, project_id)
        if mapping is None:
            continue
        if lifecycle.non_home(mapping):
            if not lifecycle.holders_on(mapping):
                # Non-home at `summaries`: this box pushes no entities at all.
                continue
            # Non-home at `full`: only this box's own `item` records. `prd`, `comment` and
            # `wait` are the home box's and the console answers them `403 not_home`.
            for row in group:
                if row.entity_type != "item":
                    _delete_if_unchanged(db, row, row.version)
            group = [row for row in group if row.entity_type == "item"]
        ready: list[tuple] = []
        for row in group:
            seen = row.version
            record = payload_entity(db, row.entity_type, row.entity_id)
            if record is None:
                continue
            manifest = db.get(SyncManifest, (row.project_id, row.entity_type, row.entity_id))
            # A hash match on an out-of-range version is not a replica. The console
            # refused that version, so skipping the send would leave the entity parked
            # in the manifest and absent from the console.
            if (manifest is not None and manifest.hash == record["hash"]
                    and version_in_range(manifest.version)):
                # The console already has this body. Sending it would be a no-op.
                # A version that moved while we looked is a newer edit, so the row stays.
                _delete_if_unchanged(db, row, seen)
                continue
            ceiling = _ceiling()
            if ceiling > 0 and gzip_size([record]) > ceiling:
                # No minute can ever carry it. Left in the queue it would sit at the
                # head forever and block every row behind it, so it parks and the
                # rest of the project keeps flushing. The 4h backstop and Retry now
                # offer it again, e.g. after an admin raises the budget.
                _park([row], reason="too_large", contract=None, now=now)
                _mark(mapping, result="refused", reason="too_large", now=now)
                continue
            ready.append((row, seen, record))
        retried = False
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
                      reason="contract", contract=version, now=now)
                _mark(mapping, result="refused", reason="contract", now=now)
                db.commit()
                return sent
            if (result.status == "refused" and result.field in _LEVEL_FIELDS
                    and not retried):
                # The console's stored level is not `full` after all. Record it, re-read the
                # choice, re-report, and send these rows once more under the level now held.
                retried = True
                lifecycle.record_refusal(db, mapping, "project-entities", reason=result.reason,
                                         field=result.field, now=now)
                lifecycle.refresh(db, transport=transport, now=now)
                mapping = db.get(SyncProject, project_id)
                rebuilt = _rebuild(db, fitted, mapping)
                ready = rebuilt + ready[len(fitted):]
                continue
            if result.status != "ok":
                _park([row for row, _, _ in fitted], reason=result.reason or "refused", contract=None,
                      now=now)
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
                    reason = refused[key]
                    # Stamp the contract on ``invalid`` so the upgrade requeue does not
                    # send the same refusal on the next pass. A contract change still
                    # unparks it, and so does the 4h backstop.
                    _park(
                        [outbox_row], reason=reason,
                        contract=advertised if reason == "invalid" else None, now=now,
                    )
                    continue
                if key not in stored:
                    continue
                _confirm(db, outbox_row, record, now)
                # The stored version decides, not the attribute: a change committed
                # during the send keeps the row dirty.
                _delete_if_unchanged(db, outbox_row, seen)
            state.failures = 0
            state.not_before = None
            _mark(mapping, result="ok", reason=None, now=now)
            sent += 1
            ready = ready[len(fitted):]
    db.commit()
    return sent
