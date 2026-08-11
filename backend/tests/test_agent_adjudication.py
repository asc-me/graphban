"""An agent may hold a quality gate; it may not approve its own work (AL-282 / PRD-14 D2).

The inconsistency this closes: `update_prd` let an agent move a PRD `draft → approved`
with nobody involved, while `publish_shard` was JWT-only with no MCP equivalent at all.
Same product, opposite stances, no principle separating them.

The resolution is an asymmetry, and it is what these tests mostly pin:

  REJECT is not an escalation — discarding your own candidate removes nothing from the
  trusted pool — so an agent does it directly.

  PUBLISH is an escalation, so an agent never performs it. It SUBMITS, and an
  independent judge decides. With no judge configured the shard stays a candidate:
  the path degrades to the human boundary, never past it.
"""
import json

import pytest

from app.services import memory as mem_svc


def _call(client, api_key: str, tool: str, args: dict) -> dict:
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(client, api_key: str, tool: str, args: dict) -> dict:
    result = _call(client, api_key, tool, args)
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _err(client, api_key: str, tool: str, args: dict) -> dict:
    result = _call(client, api_key, tool, args)
    assert result.get("isError") is True, result
    return result["structuredContent"]["error"]


@pytest.fixture()
def project(client, auth):
    r = client.post("/api/projects", json={"name": "Adjudicate", "tag": "ADJ"}, headers=auth)
    pid = r.json()["id"]
    r = client.post("/api/api-keys",
                    json={"name": "agent", "project_id": pid, "scopes": ["read", "write"]},
                    headers=auth)
    return pid, r.json()["plaintext"]


def _allow(client, auth, pid, **extra):
    r = client.patch(f"/api/projects/{pid}",
                     json={"agent_adjudication": True, **extra}, headers=auth)
    assert r.status_code == 200, r.text


def _judge(monkeypatch, keep: bool, quality: float, reason: str = "because"):
    """Stand in for a configured chat model. Without one there is no verdict, which is the
    degrade path — so a test that wants a verdict must supply one.

    Patches `judge_verdict`, which is what `agent_publish` calls since GRPH-351: it returns
    `(verdict, cause)` so a caller can report WHICH failure occurred instead of blaming a
    missing model for all of them. `_llm_judge` is now a thin view over it, and patching
    that one no longer reaches the publish path.
    """
    monkeypatch.setattr(
        mem_svc, "judge_verdict",
        lambda db, shard: ({"keep": keep, "quality": quality, "reason": reason}, "ok"))


# ---- the asymmetry ------------------------------------------------------------------
def test_an_agent_cannot_publish_without_a_judge(client, auth, project):
    """The core guarantee. No chat model is configured in tests, so submission fails —
    and the shard must be UNCHANGED, not published, not rejected."""
    pid, key = project
    _allow(client, auth, pid)
    shard = _ok(client, key, "add_memory", {"text": "A note the agent would love to publish."})

    err = _err(client, key, "publish_memory", {"shard_id": shard["id"]})
    assert err["code"] == "unavailable", err

    after = _ok(client, key, "search_memory", {"query": "publish", "include_candidates": True})
    assert after["results"][0]["status"] == "candidate", after


def test_the_judge_decides_not_the_agent(client, auth, project, monkeypatch):
    """A submitted shard the judge dislikes is REJECTED — the agent asked to publish and
    got the opposite, which is the whole point of submitting rather than publishing."""
    pid, key = project
    _allow(client, auth, pid)
    shard = _ok(client, key, "add_memory", {"text": "Vague and low signal."})
    _judge(monkeypatch, keep=False, quality=0.1, reason="too vague")

    out = _ok(client, key, "publish_memory", {"shard_id": shard["id"]})
    assert out["verdict"]["kept"] is False, out
    assert out["shard"]["status"] == "rejected", out


def test_a_kept_verdict_publishes_and_becomes_searchable(client, auth, project, monkeypatch):
    pid, key = project
    _allow(client, auth, pid)
    shard = _ok(client, key, "add_memory", {"text": "Vector indexes here are HNSW, not ivfflat."})
    _judge(monkeypatch, keep=True, quality=0.95, reason="durable and specific")

    out = _ok(client, key, "publish_memory", {"shard_id": shard["id"]})
    assert out["verdict"]["kept"] is True and out["shard"]["status"] == "published"
    found = _ok(client, key, "search_memory", {"query": "HNSW index"})
    assert [r["id"] for r in found["results"]] == [shard["id"]]


def test_a_kept_but_mediocre_verdict_does_not_publish(client, auth, project, monkeypatch):
    """`keep: true` alone is not enough — the judge's own quality bar still applies, so a
    lukewarm yes can't be used as a rubber stamp."""
    pid, key = project
    _allow(client, auth, pid)
    shard = _ok(client, key, "add_memory", {"text": "Might be worth caching this."})
    _judge(monkeypatch, keep=True, quality=0.5)

    out = _ok(client, key, "publish_memory", {"shard_id": shard["id"]})
    assert out["verdict"]["kept"] is False and out["shard"]["status"] == "rejected"


def test_an_agent_may_reject_its_own_candidate_with_no_judge(client, auth, project):
    """Rejection needs no adjudication: it takes nothing OUT of the trusted pool."""
    pid, key = project
    _allow(client, auth, pid)
    shard = _ok(client, key, "add_memory", {"text": "Superseded five minutes later."})

    out = _ok(client, key, "reject_memory", {"shard_id": shard["id"], "reason": "superseded"})
    assert out["status"] == "rejected", out


# ---- the gate is off by default -----------------------------------------------------
def test_adjudication_is_refused_unless_the_project_opts_in(client, auth, project):
    pid, key = project
    shard = _ok(client, key, "add_memory", {"text": "Nobody said the agent could do this."})
    for tool in ("publish_memory", "reject_memory"):
        err = _err(client, key, tool, {"shard_id": shard["id"]})
        assert err["code"] == "unauthorized", (tool, err)


def test_a_shard_in_another_project_is_not_found(client, auth, project):
    """Not 'forbidden' — whether a shard exists in a project you can't write to is not
    something a key should be able to probe."""
    pid, key = project
    _allow(client, auth, pid)
    other = client.post("/api/memory/shards",
                        json={"text": "Elsewhere.", "scope": "global", "project_id": "core"},
                        headers=auth).json()
    err = _err(client, key, "publish_memory", {"shard_id": other["id"]})
    assert err["code"] == "not_found", err


def test_only_a_candidate_can_be_adjudicated(client, auth, project, monkeypatch):
    pid, key = project
    _allow(client, auth, pid)
    shard = _ok(client, key, "add_memory", {"text": "Adjudicated once already."})
    _judge(monkeypatch, keep=True, quality=0.95)
    _ok(client, key, "publish_memory", {"shard_id": shard["id"]})

    err = _err(client, key, "publish_memory", {"shard_id": shard["id"]})
    assert err["code"] == "conflict", err


# ---- provenance and the corroboration pool ------------------------------------------
def test_an_agent_published_shard_is_labeled(client, auth, project, monkeypatch):
    pid, key = project
    _allow(client, auth, pid)
    shard = _ok(client, key, "add_memory", {"text": "Labelled for a later human sweep."})
    _judge(monkeypatch, keep=True, quality=0.95)
    _ok(client, key, "publish_memory", {"shard_id": shard["id"]})

    lane = client.get(f"/api/memory/auto-actions?project_id={pid}", headers=auth).json()
    assert [s["scoring_source"] for s in lane] == ["agent"], lane


@pytest.mark.parametrize("source", ["agent", "trusted"])
def test_an_unvetted_shard_never_corroborates_a_new_claim(client, auth, project, source):
    """The poisoning guard, and the reason the label is worth carrying.

    Without it, a long unattended run turns months of unreviewed shards into the pool
    that later candidates are measured against — so new junk auto-accepts for
    corroborating with old junk, and the review boundary reads as enabled while meaning
    nothing. A shard nobody vetted is still real memory and still searchable; it is
    simply not EVIDENCE that a new claim is sound."""
    from app.db import SessionLocal
    from app.models import MemoryShard

    pid, key = project
    text = "Rate limits are enforced per organization, not per key."
    published = _ok(client, key, "add_memory", {"text": text})
    db = SessionLocal()
    try:
        row = db.get(MemoryShard, published["id"])
        row.status, row.scoring_source = "published", source
        db.commit()
        # It IS in the published pool...
        assert any(s.id == row.id for s in mem_svc.list_shards(db, project_id=pid, status="published"))
        # ...and it is NOT in the pool that corroborates a new claim.
        assert all(s.id != row.id for s, _ in mem_svc._corroboration_pool(db, pid))
    finally:
        db.close()


def test_dedup_still_sees_unvetted_shards(client, auth, project):
    """The other half: excluding unvetted shards from CORROBORATION must not exclude them
    from DEDUP, or a trusted project stops detecting duplicates of its own shards and
    fills with restatements of one fact."""
    pid, key = project
    client.patch(f"/api/projects/{pid}",
                 json={"memory_write_mode": "trusted", "memory_auto_reject": True}, headers=auth)
    text = "The compose project name is pinned so the volume doesn't move."
    assert _ok(client, key, "add_memory", {"text": text})["status"] == "published"
    assert _ok(client, key, "add_memory", {"text": text})["status"] == "rejected"
