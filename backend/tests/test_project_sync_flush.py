"""GRPH-1735: drain the project-sync outbox against the in-process console.

The budget and the Retry-After floor are enforced in ``flush_once``, before any
send. Deleting that floor fails
``test_a_429_retry_after_sends_nothing_until_the_wait_is_over``.
A pass that does not call ``flush_once`` fails ``test_the_flush_pass_calls_flush_once``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, text

from app.config import Settings, settings
from app.db import SessionLocal
from app.models import SyncLink, SyncManifest, SyncOutbox, SyncProject
from app.security import secrets
from app.services import items as items_svc
from app.services import project_sync_client
from app.services import project_sync_flush as flush
from app.services import project_sync_outbox as outbox
from app.services import project_sync_payload as payload
from app.services.project_sync_client import CONTRACT
from tests.project_sync_fake import FakeConsole

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
DUE = T0 + timedelta(seconds=60)
CONSOLE = "https://console.example"
TOKEN = "gb_dep_PROJECT_SYNC_FLUSH_TOKEN_MUST_NOT_LEAK_7f3a"
WINDOW = timedelta(seconds=60)
BYTE_BUDGET = 256 * 1024


@pytest.fixture()
def db(client):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


class Recording(httpx.BaseTransport):
    """One transport for a whole flush. ``close`` does nothing, so the contract
    read and the push share it — ``httpx.Client`` closes its transport on the
    way out, and a fresh ``MockTransport`` per call would miss the second one.
    """

    def __init__(self, inner, clock: dict):
        self.inner = inner
        self.clock = clock
        self.events: list[tuple[datetime, str, int]] = []
        self.on_push = None
        self._pushed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.events.append((self.clock["now"], request.url.path, len(request.content)))
        if (self.on_push is not None and not self._pushed
                and request.method == "POST"
                and request.url.path == "/api/sync/project-entities"):
            self._pushed = True
            return self.on_push(request, self.inner)
        return self.inner.handle_request(request)

    def close(self) -> None:
        return None


class Advertised(FakeConsole):
    """GET /api/sync/contract speaks ``advertised``, not the client's major."""

    def __init__(self):
        super().__init__()
        self.advertised = "9"

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sync/contract" and self.advertised != CONTRACT:
            self.requests.append({
                "method": request.method,
                "path": request.url.path,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "params": dict(request.url.params),
            })
            return self._respond(422, json={
                "detail": "contract", "field": "version", "version": self.advertised,
            })
        return super().handle(request)


class RefuseOne(FakeConsole):
    def __init__(self, ident: str):
        super().__init__()
        self.ident = ident

    def _one_entity(self, replica, entity):
        # Refuse before the parent stores it. A reason applied afterwards still
        # leaves the entity in the replica, which reads as accepted.
        if isinstance(entity, dict) and entity.get("id") == self.ident:
            version = entity.get("version")
            return {
                "type": entity.get("type") if isinstance(entity.get("type"), str) else "",
                "id": self.ident,
                "version": version if isinstance(version, int) and not isinstance(version, bool) else 0,
            }, "forbidden_type"
        return super()._one_entity(replica, entity)


class _Box:
    def __init__(self, db, fake):
        self.db = db
        self.fake = fake
        fake.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
        db.add(SyncLink(id="instance", console_url=CONSOLE, cloud_url=CONSOLE,
                        token_enc=secrets.encrypt(TOKEN)))
        db.commit()
        created = project_sync_client.register_project(
            db, tag="GRPH", name="Graphban", transport=fake.transport())
        assert created.status == "ok" and created.cloud_project_id
        self.cloud_id = created.cloud_project_id
        db.add(SyncProject(
            local_project_id="core", cloud_project_id=self.cloud_id,
            sync_level=outbox.SYNC_LEVEL_SYNC,
        ))
        db.commit()
        self.clock = {"now": T0}
        self.wire = Recording(fake.transport(), self.clock)
        self.state = flush.FlushState()

    def at(self, when: datetime) -> int:
        self.clock["now"] = when
        return flush.flush_once(
            self.db, now=when, transport=self.wire, state=self.state, rng=lambda: 0,
        )


def _item(db, title: str, status: str = "next"):
    return items_svc.create_item(db, project_id="core", title=title, status=status)


def _dirty(db, item, when: datetime, status: str | None = None):
    if status is not None:
        item.status = status
    outbox.capture(
        db, project_id="core", entity_type="item", entity_id=item.key,
        change_class=outbox.CHANGE_STATUS, now=when,
    )
    db.commit()


def _row(db, item):
    db.expire_all()
    return db.get(SyncOutbox, ("core", "item", item.key))


def _manifest(db, item):
    db.expire_all()
    return db.get(SyncManifest, ("core", "item", item.key))


def _pushes(events):
    return [event for event in events if event[1] == "/api/sync/project-entities"]


def _window(events, now: datetime):
    cutoff = now - WINDOW
    # Same strict edge as the token bucket: a send exactly 60s ago has left the window.
    return [event for event in events if cutoff < event[0] <= now]


def test_the_suite_leaves_the_flush_loop_off():
    assert settings.sync_flush_seconds == 0
    assert Settings.model_fields["sync_flush_seconds"].default == 5
    assert Settings.model_fields["sync_flush_bytes_per_min"].default == BYTE_BUDGET
    assert Settings.model_fields["sync_flush_requests_per_min"].default == 30


def test_an_empty_outbox_does_not_call_the_console(db):
    box = _Box(db, FakeConsole())
    assert box.at(DUE) == 0
    assert box.wire.events == []


def test_five_hundred_status_changes_over_ten_minutes_stay_inside_the_budget(db):
    """One entity, edited the whole ten minutes. Coalescing plus the 60s max delay
    is what keeps this to a handful of pushes; the bucket is what would stop a
    second push inside the same minute. The contract read rides along in the same
    tick, so the ceiling on every request is the 30/min budget, and the ceiling
    on entity posts is one per 60s.
    """
    box = _Box(db, FakeConsole())
    item = _item(db, "budget")
    step = timedelta(seconds=600) / 500
    for i in range(500):
        when = T0 + step * i
        _dirty(db, item, when, status="next" if i % 2 == 0 else "in_progress")
        box.at(when)
    pushes = _pushes(box.wire.events)
    assert len(pushes) == 9
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(pushes, pushes[1:])]
    assert gaps and min(gaps) > 60
    for at, _path, _size in box.wire.events:
        window = _window(box.wire.events, at)
        assert len(window) <= 30
        posted = [event for event in window if event[1] == "/api/sync/project-entities"]
        assert len(posted) <= 1
        assert sum(event[2] for event in posted) <= BYTE_BUDGET
    assert _row(db, item) is not None  # the tail is not due yet; it was not dropped


def test_the_request_budget_holds_the_rest_until_the_window_moves(db, monkeypatch):
    monkeypatch.setattr(flush, "BATCH_ENTITIES", 1)
    monkeypatch.setattr(settings, "sync_flush_requests_per_min", 1)
    box = _Box(db, FakeConsole())
    first = _item(db, "one")
    second = _item(db, "two")
    _dirty(db, first, T0)
    _dirty(db, second, T0)
    assert box.at(DUE) == 1
    assert box.at(DUE) == 0
    assert len(_pushes(box.wire.events)) == 1
    assert box.at(DUE + WINDOW) == 1
    assert len(_pushes(box.wire.events)) == 2
    assert _row(db, first) is None and _row(db, second) is None


def test_the_byte_budget_holds_the_next_entity_until_the_window_moves(db, monkeypatch):
    box = _Box(db, FakeConsole())
    first = _item(db, "one")
    second = _item(db, "two")
    _dirty(db, first, T0)
    _dirty(db, second, T0)
    records = [payload.entity(db, "item", item.key) for item in (first, second)]
    alone = [flush.gzip_size([record]) for record in records]
    pair = flush.gzip_size(records)
    # The ceiling fits either entity on its own and does not fit the pair.
    # Pinning it to the smaller one leaves the larger dirty forever.
    limit = max(alone)
    assert pair > limit and all(size <= limit for size in alone)
    monkeypatch.setattr(settings, "sync_flush_bytes_per_min", limit)
    assert box.at(DUE) == 1
    assert box.at(DUE) == 0
    assert box.at(DUE + WINDOW) == 1
    assert _manifest(db, first) is not None and _manifest(db, second) is not None


def test_a_429_retry_after_sends_nothing_until_the_wait_is_over(db):
    box = _Box(db, FakeConsole())
    item = _item(db, "held")
    _dirty(db, item, T0)
    box.fake.retry_after_once(30)
    assert box.at(DUE) == 0
    assert [event[1] for event in box.wire.events] == ["/api/sync/contract"]
    assert box.state.not_before == DUE + timedelta(seconds=30)
    assert _row(db, item) is not None
    assert (_row(db, item).parked_reason or "") == ""
    held = len(box.wire.events)
    assert box.at(DUE + timedelta(seconds=29)) == 0
    assert len(box.wire.events) == held  # nothing reaches the console during the wait
    assert box.at(DUE + timedelta(seconds=30)) == 1
    pushes = _pushes(box.wire.events)
    assert len(pushes) == 1 and pushes[0][0] == DUE + timedelta(seconds=30)
    assert _row(db, item) is None and _manifest(db, item) is not None


@pytest.mark.parametrize("fault", ["timeout", "503"])
def test_a_timeout_leaves_rows_dirty_and_unparked(db, fault):
    box = _Box(db, FakeConsole())
    item = _item(db, "blip")
    other = _item(db, "neighbour")
    _dirty(db, item, T0)
    _dirty(db, other, T0)

    def fail(request, inner):
        del request, inner
        if fault == "timeout":
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(503, json={"detail": "down"})

    box.wire.on_push = fail
    assert box.at(DUE) == 0
    for entity in (item, other):
        row = _row(db, entity)
        assert row is not None
        assert (row.parked_reason or "") == ""
        assert row.attempts >= 1
        assert _manifest(db, entity) is None
    mapping = db.get(SyncProject, "core")
    assert mapping.last_push_result == "unknown"


def test_a_refused_entity_parks_and_does_not_block_the_others(db):
    kept = _item(db, "kept")
    blocked = _item(db, "blocked")
    box = _Box(db, RefuseOne(blocked.key))
    _dirty(db, kept, T0)
    _dirty(db, blocked, T0)
    assert box.at(DUE) == 1
    assert _row(db, kept) is None
    assert _manifest(db, kept) is not None
    refused = _row(db, blocked)
    assert refused is not None and refused.parked_reason == "forbidden_type"
    assert refused.parked_contract is None
    assert _manifest(db, blocked) is None
    stored = box.fake.entities(box.cloud_id)
    assert [row["id"] for row in stored] == [kept.key]


def test_a_422_contract_parks_until_the_advertised_version_changes(db):
    box = _Box(db, Advertised())
    item = _item(db, "contract")
    _dirty(db, item, T0)
    assert box.at(DUE) == 0
    row = _row(db, item)
    assert row.parked_reason == "contract" and row.parked_contract == "9"
    assert _manifest(db, item) is None
    assert _pushes(box.wire.events) == []
    # Same advertisement: still parked, still no push.
    assert box.at(DUE + timedelta(minutes=1)) == 0
    assert _row(db, item).parked_contract == "9"
    assert _pushes(box.wire.events) == []
    box.fake.advertised = CONTRACT
    assert box.at(DUE + timedelta(minutes=2)) == 1
    assert _row(db, item) is None
    assert _manifest(db, item) is not None
    assert len(_pushes(box.wire.events)) == 1


def test_parked_rows_requeue_after_four_hours(db):
    box = _Box(db, FakeConsole())
    item = _item(db, "stale contract")
    _dirty(db, item, T0)

    def once(request, inner):
        del request, inner
        return httpx.Response(422, json={
            "detail": "contract", "field": "contract", "version": "1",
        })

    box.wire.on_push = once
    assert box.at(DUE) == 0
    assert _row(db, item).parked_contract == "1"
    assert box.at(T0 + timedelta(hours=1)) == 0
    assert _row(db, item).parked_reason == "contract"
    assert len(_pushes(box.wire.events)) == 1  # the refusal itself; nothing since
    # The backstop counts from the park (DUE), not from when the row first went dirty.
    assert box.at(T0 + timedelta(hours=4)) == 0
    assert len(_pushes(box.wire.events)) == 1
    assert box.at(DUE + timedelta(hours=4)) == 1
    assert _row(db, item) is None
    assert _manifest(db, item) is not None
    assert len(_pushes(box.wire.events)) == 2


def test_retry_parked_clears_the_park(db):
    box = _Box(db, Advertised())
    item = _item(db, "retry")
    _dirty(db, item, T0)
    assert box.at(DUE) == 0
    assert flush.retry_parked(db, "") == 0
    assert _row(db, item).parked_reason == "contract"
    assert flush.retry_parked(db, "no-such-project") == 0
    assert _row(db, item).parked_reason == "contract"
    assert flush.retry_parked(db, "core") == 1
    row = _row(db, item)
    assert (row.parked_reason or "") == "" and row.parked_contract is None
    box.fake.advertised = CONTRACT
    assert box.at(DUE + timedelta(seconds=1)) == 1
    assert _manifest(db, item) is not None


def test_an_edit_during_send_stays_dirty_after_confirm(db):
    box = _Box(db, FakeConsole())
    item = _item(db, "before")
    _dirty(db, item, T0)
    sent_hash = payload.entity(db, "item", item.key)["hash"]

    def edit(request, inner):
        item.title = "changed while the bytes were in flight"
        row = db.get(SyncOutbox, ("core", "item", item.key))
        row.version = row.version + 1
        db.flush()
        return inner.handle_request(request)

    box.wire.on_push = edit
    assert box.at(DUE) == 1
    row = _row(db, item)
    assert row is not None  # the version moved; the confirm must not delete it
    manifest = _manifest(db, item)
    assert manifest is not None and manifest.hash == sent_hash
    assert payload.entity(db, "item", item.key)["hash"] != sent_hash
    stored = box.fake.entities(box.cloud_id)
    assert stored[0]["body"]["title"] == "before"
    assert box.at(DUE) == 1
    assert _row(db, item) is None
    assert _manifest(db, item).hash == payload.entity(db, "item", item.key)["hash"]
    titles = [row["body"]["title"] for row in box.fake.entities(box.cloud_id)]
    assert "changed while the bytes were in flight" in titles


def test_a_still_refused_row_waits_another_four_hours_after_each_retry(db):
    item = _item(db, "always refused")
    box = _Box(db, RefuseOne(item.key))
    _dirty(db, item, T0)
    assert box.at(DUE) == 1  # the batch was accepted; this entity was refused
    assert _row(db, item).parked_reason == "forbidden_type"
    assert len(_pushes(box.wire.events)) == 1
    assert box.at(DUE + timedelta(hours=4) - timedelta(seconds=1)) == 0
    assert len(_pushes(box.wire.events)) == 1
    assert box.at(DUE + timedelta(hours=4)) == 1  # the backstop retry
    assert len(_pushes(box.wire.events)) == 2
    assert _row(db, item).parked_reason == "forbidden_type"  # refused again: re-parked
    # The re-park re-arms the backstop. A stale first stamp would send on every pass.
    for minutes in (1, 5, 60, 239):
        assert box.at(DUE + timedelta(hours=4, minutes=minutes)) == 0
    assert len(_pushes(box.wire.events)) == 2
    assert box.at(DUE + timedelta(hours=8)) == 1
    assert len(_pushes(box.wire.events)) == 3


def test_an_edit_committed_by_another_session_during_send_stays_dirty(db):
    box = _Box(db, FakeConsole())
    item = _item(db, "before")
    _dirty(db, item, T0)
    key = ("core", "item", item.key)
    sent_version = db.get(SyncOutbox, key).version

    def edit_elsewhere(request, inner):
        other = SessionLocal()
        try:
            outbox.capture(
                other, project_id="core", entity_type="item", entity_id=item.key,
                change_class=outbox.CHANGE_EDIT, now=DUE,
            )
            other.commit()
        finally:
            other.close()
        return inner.handle_request(request)

    box.wire.on_push = edit_elsewhere
    assert box.at(DUE) == 1
    row = _row(db, item)
    assert row is not None  # the newer version is still queued
    assert row.version == sent_version + 1
    assert _manifest(db, item) is not None


def test_a_version_bump_the_identity_map_did_not_see_stays_dirty(db):
    box = _Box(db, FakeConsole())
    item = _item(db, "before")
    _dirty(db, item, T0)

    def raw_bump(request, inner):
        db.execute(
            text("UPDATE sync_outbox SET version = version + 1 "
                 "WHERE project_id = 'core' AND entity_type = 'item' AND entity_id = :eid"),
            {"eid": item.key},
        )
        return inner.handle_request(request)

    box.wire.on_push = raw_bump
    assert box.at(DUE) == 1
    assert _row(db, item) is not None


def test_an_unchanged_entity_is_not_sent_again(db):
    box = _Box(db, FakeConsole())
    item = _item(db, "stable")
    _dirty(db, item, T0)
    assert box.at(DUE) == 1
    _dirty(db, item, DUE)  # a new outbox row, the same body
    assert box.at(DUE + WINDOW) == 0
    assert len(_pushes(box.wire.events)) == 1
    assert _row(db, item) is None
    assert _manifest(db, item) is not None


def test_the_flush_pass_calls_flush_once(monkeypatch, client):
    seen = {}

    def fake_flush(db, *, now, transport=None, state=None, rng=None):
        seen["now"] = now
        seen["called"] = True
        return 3

    monkeypatch.setattr(flush, "flush_once", fake_flush)
    from app.main import _project_sync_flush_pass

    assert _project_sync_flush_pass() == 3
    assert seen.get("called") is True and seen["now"] is not None


def test_retry_parked_on_an_empty_project_is_zero_not_a_wipe(db):
    box = _Box(db, Advertised())
    item = _item(db, "stays")
    _dirty(db, item, T0)
    box.at(DUE)
    assert flush.retry_parked(db, "") == 0
    assert db.scalars(select(SyncOutbox).where(SyncOutbox.parked_reason == "contract")).all()


def _incompressible(size: int) -> str:
    import base64
    import os
    return base64.b64encode(os.urandom(size)).decode()


def test_an_entity_too_big_for_an_empty_minute_parks_and_the_rest_still_flush(db):
    """At the D11 defaults (256 KB/min, 1 MiB batches) a body over 256 KB can never
    go out. It parks instead of sitting at the head of the queue, and the smaller
    entity behind it sends in the same pass.
    """
    box = _Box(db, FakeConsole())
    big = items_svc.create_item(
        db, project_id="core", title="big", status="next",
        description=_incompressible(300 * 1024),
    )
    small = _item(db, "small")
    _dirty(db, big, T0)
    _dirty(db, small, T0)
    assert flush.gzip_size([payload.entity(db, "item", big.key)]) > BYTE_BUDGET
    assert box.at(DUE) == 1
    assert _row(db, small) is None and _manifest(db, small) is not None
    parked = _row(db, big)
    assert parked is not None and parked.parked_reason == "too_large"
    assert parked.parked_contract is None
    assert _manifest(db, big) is None
    pushes = _pushes(box.wire.events)
    assert len(pushes) == 1 and all(size <= BYTE_BUDGET for _, _, size in pushes)
    assert [row["id"] for row in box.fake.entities(box.cloud_id)] == [small.key]
    # Parked, it no longer holds the queue: a later edit to another entity sends.
    other = _item(db, "later")
    _dirty(db, other, DUE + WINDOW)
    assert box.at(DUE + WINDOW * 2) == 1
    assert _manifest(db, other) is not None


def test_a_parked_oversized_entity_sends_once_the_budget_can_carry_it(db, monkeypatch):
    box = _Box(db, FakeConsole())
    big = items_svc.create_item(
        db, project_id="core", title="big", status="next",
        description=_incompressible(300 * 1024),
    )
    _dirty(db, big, T0)
    assert box.at(DUE) == 0
    assert _row(db, big).parked_reason == "too_large"
    assert _pushes(box.wire.events) == []
    monkeypatch.setattr(settings, "sync_flush_bytes_per_min", 1024 * 1024)
    assert flush.retry_parked(db, "core") == 1
    assert box.at(DUE + WINDOW) == 1
    assert _row(db, big) is None and _manifest(db, big) is not None
