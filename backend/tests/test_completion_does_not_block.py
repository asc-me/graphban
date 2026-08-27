"""Completing an item must not wait on a model (GRPH-399).

`update_item(status="done")` fired two synchronous model calls — the platform judge and the
lesson extractor. Measured on the live instance: a trivial prompt to its 24B chat model takes
20s, each call is bounded by `llm_timeout_seconds = 90`, so a completion could block ~180s.

Presence TTL is 150s and a fleet agent is single-threaded, so **completing an item could push
an agent past its own TTL and take it offline**, releasing the rest of its work. Both call
sites were already careful that a failure could not break the completion; nothing stopped them
DELAYING it.
"""
import json
import time

import pytest

from app.services import items as items_svc
from app.services import platform as platform_svc
from tests import attest


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "NoBlock"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    """Carries `gate` because these tests complete an item over MCP, and completing needs an
    `attestation` — which only a gate-scoped key may write (GRPH-541/543). Nothing here is
    about authority; the scope is what makes the transition reachable at all."""
    return client.post("/api/api-keys",
                       json={"name": "a", "project_id": proj,
                             "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


class _SlowExtractor:
    """Stands in for the 24B model on the live box, at 1/10th the measured latency."""

    def __init__(self, seconds=2.0):
        self.seconds = seconds
        self.ran = False

    def extract(self, *, title, description):
        time.sleep(self.seconds)
        self.ran = True
        return ["a lesson"]


def _done_over_mcp(client, key, item_id):
    return client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "update_item",
                   "arguments": {"id": item_id, **attest.complete_body()}},
    }, headers={"X-API-Key": key})


def test_the_service_hands_the_model_calls_to_the_scheduler(client, auth, proj, db,
                                                            monkeypatch):
    """THE regression, asserted where it is observable. Given a scheduler, `update_item`
    returns without running the model at all — that gap is what keeps a single-threaded agent
    heartbeating while its completion is digested.

    Not asserted through the test client: Starlette runs a background task before the client
    returns, so an HTTP-level timing test measures the very thing it is trying to prove is off
    the response path. That property is what keeps the extraction tests deterministic, so it is
    worth more than the timing assertion it costs."""
    slow = _SlowExtractor(2.0)
    monkeypatch.setattr(platform_svc, "extractor_for", lambda db, pid: slow)
    item = client.post("/api/items", json={"title": "work", "project_id": proj},
                       headers=auth).json()
    jobs: list = []

    started = time.monotonic()
    attest.complete(db, item["id"], defer=jobs.append)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, (
        f"the completion waited {elapsed:.1f}s on the extractor — with the real model that is "
        "up to 180s against a 150s presence TTL")
    assert not slow.ran, "the model must not have been called yet"
    assert len(jobs) == 1
    jobs[0]()
    assert slow.ran, "and the scheduled job must be the one that does it"


def test_both_web_callers_pass_a_scheduler(client, auth, proj, key, monkeypatch):
    """The wiring, which is the half a service-level test cannot see. If either caller stops
    passing `defer`, the default is INLINE — so the fix silently reverts and the only symptom
    is agents going quiet again."""
    seen: dict = {}
    real = items_svc.update_item

    def spy(db, item_id, defer=None, **fields):
        seen[fields.get("_via", "call")] = defer
        return real(db, item_id, defer=defer, **fields)

    monkeypatch.setattr(items_svc, "update_item", spy)
    monkeypatch.setattr("app.mcp_server.items_svc.update_item", spy)
    monkeypatch.setattr("app.routers.items.items_svc.update_item", spy)
    item = client.post("/api/items", json={"title": "work", "project_id": proj},
                       headers=auth).json()

    _done_over_mcp(client, key, item["id"])
    assert callable(seen.get("call")), "the MCP dispatcher must schedule, not run inline"

    seen.clear()
    client.patch(f"/api/items/{item['id']}", json={"status": "next"}, headers=auth)
    assert callable(seen.get("call")), "the REST endpoint must schedule too"


def test_the_enrichment_still_runs(client, auth, proj, key, monkeypatch):
    """Deferred, not dropped. Moving work off the response path is only correct if the work
    still happens — and Starlette runs the background task before the test client returns, so
    this is observable rather than raced."""
    slow = _SlowExtractor(0.01)
    monkeypatch.setattr(platform_svc, "extractor_for", lambda db, pid: slow)
    item = client.post("/api/items", json={"title": "work", "project_id": proj},
                       headers=auth).json()

    _done_over_mcp(client, key, item["id"])

    assert slow.ran, "the extractor must still have been called"
    shards = client.get(f"/api/memory/shards?project_id={proj}", headers=auth).json()
    assert any(s["source"] == f"lesson from {item['id']}" for s in shards)


def test_deferred_work_that_throws_cannot_reach_the_client(client, auth, proj, key,
                                                           monkeypatch):
    """The response has already been sent, so an exception in the scheduled work has nowhere
    to go — it must be swallowed and logged rather than surfacing as a failed completion.

    Reached by making the SCHEDULED CALL throw rather than the extractor: `_auto_extract_lessons`
    and `_classify_against_goal` both already swallow their own failures, so an exploding
    extractor never gets near this guard. The failures it exists for are the ones outside them
    — the session, the item lookup — and a first draft that boomed the extractor passed with
    the guard deleted."""
    def boom(item_id):
        raise RuntimeError("session is gone")

    monkeypatch.setattr("app.services.items.enrich_completed_item", boom)
    item = client.post("/api/items", json={"title": "work", "project_id": proj},
                       headers=auth).json()

    r = _done_over_mcp(client, key, item["id"])

    assert json.loads(r.json()["result"]["content"][0]["text"])["status"] == "done"


def test_the_service_still_enriches_inline_by_default(client, auth, proj, db, monkeypatch):
    """`defer` defaults to inline, so every caller that is not a web request — scripts,
    services, the seed — keeps today's ordering rather than silently losing the enrichment to
    a scheduler that is not there."""
    slow = _SlowExtractor(0.01)
    monkeypatch.setattr(platform_svc, "extractor_for", lambda db, pid: slow)
    item = client.post("/api/items", json={"title": "inline", "project_id": proj},
                       headers=auth).json()

    attest.complete(db, item["id"])

    assert slow.ran, "with no scheduler the work must happen in the call"
