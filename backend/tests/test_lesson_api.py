"""PR 2 callers: lessons detail/outcomes/promote, miss-on-publish, evidence kind=lesson.

Sabotage the CALL. Deleting maybe_record_recurrence_miss from set_status must fail
the set_status test; deleting it from agent_publish must fail independently.
Putting get_lessons in _PAGED must drop caught_state from the advertised schema.
"""
from __future__ import annotations

import json
import math

import pytest
from sqlalchemy import select

from app.models import CodeNode, LessonOutcome, MemoryShard
from app.services import items as items_svc
from app.services import memory as mem_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _mcp(client, key, tool, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def _key(client, auth, **body):
    payload = {"name": body.pop("name", "lessons"), **body}
    return client.post("/api/api-keys", json=payload, headers=auth).json()["plaintext"]


def _vec_pair(sim=0.90, dim=None):
    from app.config import settings

    dim = dim or settings.embed_dim
    a = [0.0] * dim
    a[0] = 1.0
    b = [0.0] * dim
    b[0] = sim
    b[1] = math.sqrt(max(0.0, 1.0 - sim * sim))
    return a, b


def _force_sim(db, older, newer, sim=0.90):
    a, b = _vec_pair(sim)
    older.embedding = a
    newer.embedding = b
    db.commit()
    db.refresh(older)
    db.refresh(newer)


def _outcomes(db, shard_id):
    return list(db.scalars(
        select(LessonOutcome).where(LessonOutcome.shard_id == shard_id)
    ).all())


CLUSTER = (
    "PR2-CLUSTER-NEEDLE three independent observations of the same correction "
    "about never promoting an unattributed ingest collapse as a counted project"
)


def _three_eligible(client, auth, db, text=CLUSTER):
    p2 = client.post("/api/projects", json={"name": "PR2 Two"}, headers=auth).json()["id"]
    p3 = client.post("/api/projects", json={"name": "PR2 Three"}, headers=auth).json()["id"]
    shards = []
    for pid in ("core", p2, p3):
        shards.append(mem_svc.add_memory(
            db, text_body=text, project_id=pid, status="published",
            origin="user:ascme", actor_user_id="u1", attributed_project_id=pid,
        ))
    return shards, p2, p3


# ---- MCP schema / JSON-RPC --------------------------------------------------------------


def test_get_lessons_is_not_paged():
    """_PAGED replaces outputSchema with {results,total,limit,offset,has_more} only
    and would drop caught_state / effectiveness. This is the advertised contract."""
    from app import mcp_server as m

    assert "get_lessons" not in m._PAGED
    assert "get_lessons" in m._READ_ONLY
    assert "get_lessons" in m._PROJECT_SCOPED
    assert "get_lessons" in m._TAKES_PROJECT
    tool = next(t for t in m.TOOLS if t["name"] == "get_lessons")
    schema = tool["outputSchema"]
    dumped = json.dumps(schema)
    assert "caught_state" in dumped
    assert "effectiveness" in dumped
    items = schema["properties"]["results"]["items"]
    assert "caught_state" in items["required"]
    assert "effectiveness" in items["required"]
    assert "eligibility" in items["required"]
    score = items["properties"]["effectiveness"]["properties"]["score"]
    assert "null" in score["type"]


def test_mcp_get_lessons_empty_catalog(client, auth):
    pid = client.post("/api/projects", json={"name": "Empty Lessons MCP"}, headers=auth).json()["id"]
    key = _key(client, auth, project_id=pid, scopes=["read", "write"])
    body = _mcp(client, key, "get_lessons", {"project_id": pid})
    assert body["results"] == []
    assert body["total"] == 0
    assert "score" not in body
    assert body["enums"]["caught_states"] == list(mem_svc.CAUGHT_STATES)


def test_mcp_get_lessons_unmeasured_score_is_null_not_omitted(client, auth, db):
    pid = client.post("/api/projects", json={"name": "Unmeasured MCP"}, headers=auth).json()["id"]
    key = _key(client, auth, project_id=pid, scopes=["read", "write"])
    shard = mem_svc.add_memory(
        db, text_body="unmeasured published lesson for mcp", project_id=pid,
        status="published", actor_user_id="u1", attributed_project_id=pid,
    )
    body = _mcp(client, key, "get_lessons", {"project_id": pid})
    row = next(r for r in body["results"] if r["id"] == shard.id)
    assert "score" in row["effectiveness"]
    assert row["effectiveness"]["score"] is None
    assert row["caught_state"] == "unknown"
    assert row["effectiveness"]["trend"] == "unmeasured"
    raw = json.dumps(row["effectiveness"])
    assert ": null" in raw.replace("null,", "null") or '"score": null' in raw


# ---- REST detail / outcomes / promote ---------------------------------------------------


def test_get_lesson_detail_has_history_and_origin_path(client, auth, db):
    shard = mem_svc.add_memory(
        db, text_body="detail lesson", project_id="core", status="published",
        actor_user_id="u1", attributed_project_id="core",
    )
    r = client.get(f"/api/memory/lessons/{shard.id}?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == shard.id
    assert body["origin_path"] in mem_svc.ORIGIN_PATH_STATES
    assert "history" in body["effectiveness"]
    assert body["effectiveness"]["history"] == []
    assert body["effectiveness"]["score"] is None
    assert body["caught_state"] == "unknown"
    assert isinstance(body["cluster"], list)
    assert isinstance(body["unread_cluster_tags"], list)
    assert isinstance(body["outcomes"], list)
    assert isinstance(body["events"], list)


def test_project_local_of_another_project_404s(client, auth, db):
    other = client.post("/api/projects", json={"name": "Other Local"}, headers=auth).json()["id"]
    shard = mem_svc.add_memory(
        db, text_body="stays in other", project_id=other, status="published",
        actor_user_id="u1", attributed_project_id=other,
    )
    r = client.get(f"/api/memory/lessons/{shard.id}?project_id=core", headers=auth)
    assert r.status_code == 404


def test_org_reach_sibling_detail_200s(client, auth, db):
    other = client.post("/api/projects", json={"name": "Org Sibling"}, headers=auth).json()["id"]
    shard = mem_svc.add_memory(
        db, text_body="org reach sibling detail", project_id=other, status="published",
        actor_user_id="u1", attributed_project_id=other,
    )
    shard.reach = "org"
    db.commit()
    r = client.get(f"/api/memory/lessons/{shard.id}?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == shard.id


def test_record_outcome_caught(client, auth, db):
    shard = mem_svc.add_memory(
        db, text_body="human marks caught", project_id="core", status="published",
        actor_user_id="u1", attributed_project_id="core",
    )
    r = client.post(
        f"/api/memory/lessons/{shard.id}/outcomes",
        json={"kind": "caught", "detail": "saw it fire"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["caught_state"] == "caught"
    assert body["effectiveness"]["score"] == 1.0
    kinds = [o["kind"] for o in body["outcomes"]]
    assert "caught" in kinds


def test_empty_caught_detail_is_422(client, auth, db):
    """Empty caught is a 1.0 trophy with no sentence. Detail is required."""
    shard = mem_svc.add_memory(
        db, text_body="empty caught must not score", project_id="core",
        status="published", actor_user_id="u1", attributed_project_id="core",
    )
    r = client.post(
        f"/api/memory/lessons/{shard.id}/outcomes",
        json={"kind": "caught", "detail": ""},
        headers=auth,
    )
    assert r.status_code == 422, r.text
    assert _outcomes(db, shard.id) == []


def test_global_shard_outcome_reloads_instead_of_404(client, auth, db):
    """viewer_project_id must not be "". The write already committed."""
    shard = mem_svc.add_memory(
        db, text_body="global published lesson outcome", project_id=None,
        status="published", actor_user_id="u1",
    )
    assert shard.project_id is None
    r = client.post(
        f"/api/memory/lessons/{shard.id}/outcomes",
        json={"kind": "caught", "detail": "it fired in core too"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["caught_state"] == "caught"
    assert r.json()["effectiveness"]["score"] == 1.0
    assert len(_outcomes(db, shard.id)) == 1


def test_promote_unverifiable_422_with_and_without_override(client, auth, db):
    shard = mem_svc.add_memory(
        db, text_body="ingest collapse still unmeasured", project_id="core",
        status="published", origin="ingest:claude-code:done",
        actor_user_id="u1", attributed_project_id=None,
    )
    r = client.post(f"/api/memory/lessons/{shard.id}/promote-org", json={}, headers=auth)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["state"] == "unverifiable"
    db.refresh(shard)
    assert shard.reach == "project"

    r2 = client.post(
        f"/api/memory/lessons/{shard.id}/promote-org",
        json={"override_reason": "I looked, trust me"},
        headers=auth,
    )
    assert r2.status_code == 422, r2.text
    assert r2.json()["detail"]["state"] == "unverifiable"
    db.refresh(shard)
    assert shard.reach == "project"


def test_ineligible_override_stamps_overridden_not_evidenced(client, auth, db):
    shard = mem_svc.add_memory(
        db, text_body="one observation is not three", project_id="core",
        status="published", origin="user:ascme",
        actor_user_id="u1", attributed_project_id="core",
    )
    r = client.post(f"/api/memory/lessons/{shard.id}/promote-org", json={}, headers=auth)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["state"] == "ineligible"

    r2 = client.post(
        f"/api/memory/lessons/{shard.id}/promote-org",
        json={"override_reason": "solo install, I still want this org-wide"},
        headers=auth,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["reach"] == "org"
    assert body["transferability"] == "overridden"
    assert body["transferability"] != "evidenced"
    db.refresh(shard)
    assert shard.reach == "org"

    again = client.post(
        f"/api/memory/lessons/{shard.id}/promote-org",
        json={"override_reason": "second time"},
        headers=auth,
    )
    assert again.status_code == 200
    # Idempotent: still overridden, no second Event required to keep the stamp.
    assert again.json()["transferability"] == "overridden"


def test_contradicted_promote_422_without_override(client, auth, db):
    shards, _, _ = _three_eligible(client, auth, db, text=CLUSTER + " contradicted")
    shard = shards[0]
    mem_svc.record_outcome(db, shard.id, kind="contradicted", source="human", detail="wrong")
    r = client.post(f"/api/memory/lessons/{shard.id}/promote-org", json={}, headers=auth)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["blocked_by"] == "effectiveness"
    db.refresh(shard)
    assert shard.reach == "project"

    r2 = client.post(
        f"/api/memory/lessons/{shard.id}/promote-org",
        json={"override_reason": "spread it anyway"},
        headers=auth,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["transferability"] == "overridden"
    assert r2.json()["transferability"] != "evidenced"


def _three_in(client, auth, db, home: str, text: str):
    p2 = client.post("/api/projects", json={"name": f"{text[:12]} Two"}, headers=auth).json()["id"]
    p3 = client.post("/api/projects", json={"name": f"{text[:12]} Three"}, headers=auth).json()["id"]
    shards = []
    for pid in (home, p2, p3):
        shards.append(mem_svc.add_memory(
            db, text_body=text, project_id=pid, status="published",
            origin="user:ascme", actor_user_id="u1", attributed_project_id=pid,
        ))
    return shards


def test_gone_empty_promote_422_unindexed_empty_200(client, auth, db):
    """Unmeasured is score is None AND trend != dropping. gone+empty is dropping."""
    gone_home = client.post("/api/projects", json={"name": "Gone Home"}, headers=auth).json()["id"]
    shards = _three_in(client, auth, db, gone_home, CLUSTER + " gone path")
    item = items_svc.create_item(
        db, title="claimed gone", project_id=gone_home,
        touchpoints=["app/services/memory.py"],
    )
    db.add(CodeNode(
        id="cn_pr2_other", project_id=gone_home, path="web/src/App.tsx",
        kind="file", name="App",
    ))
    shards[0].item_id = item.id
    db.commit()
    assert mem_svc.origin_path_state(db, shards[0]) == "gone"
    r = client.post(f"/api/memory/lessons/{shards[0].id}/promote-org", json={}, headers=auth)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["blocked_by"] == "effectiveness"
    db.refresh(shards[0])
    assert shards[0].reach == "project"

    idx_home = client.post("/api/projects", json={"name": "Unindexed Home"}, headers=auth).json()["id"]
    u_shards = _three_in(client, auth, db, idx_home, CLUSTER + " unindexed path")
    u_item = items_svc.create_item(
        db, title="claimed unindexed", project_id=idx_home,
        touchpoints=["does/not/exist.py"],
    )
    u_shards[0].item_id = u_item.id
    db.commit()
    assert mem_svc.origin_path_state(db, u_shards[0]) == "unindexed"
    r2 = client.post(f"/api/memory/lessons/{u_shards[0].id}/promote-org", json={}, headers=auth)
    assert r2.status_code == 200, r2.text
    assert r2.json()["reach"] == "org"
    assert r2.json()["transferability"] == "evidenced"


def test_promote_org_rejects_api_key(client, auth, db):
    shard = mem_svc.add_memory(
        db, text_body="agents cannot promote", project_id="core", status="published",
        actor_user_id="u1", attributed_project_id="core",
    )
    key = _key(client, auth, scopes=["read", "write"])
    r = client.post(
        f"/api/memory/lessons/{shard.id}/promote-org",
        json={},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 401


def test_three_human_publishes_are_eligible_before_ingest_mapping(client, auth, db):
    shards, _, _ = _three_eligible(client, auth, db)
    r = client.get(f"/api/memory/lessons/{shards[0].id}?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    elig = r.json()["eligibility"]
    assert elig["state"] == "eligible", elig
    assert elig["independence"] == 3
    listed = client.get("/api/memory/lessons?project_id=core", headers=auth).json()
    row = next(x for x in listed["results"] if x["id"] == shards[0].id)
    assert row["eligibility"]["state"] == "eligible"
    promo = client.post(f"/api/memory/lessons/{shards[0].id}/promote-org", json={}, headers=auth)
    assert promo.status_code == 200, promo.text
    assert promo.json()["reach"] == "org"
    assert promo.json()["transferability"] == "evidenced"


def test_unreadable_sibling_is_unread_project_never_tag_or_text(client, auth, db):
    secret_text = CLUSTER + " unread secret"
    shard = mem_svc.add_memory(
        db, text_body=secret_text, project_id="core", status="published",
        origin="user:ascme", actor_user_id="u1", attributed_project_id="core",
    )
    other = client.post(
        "/api/auth/register",
        json={"name": "Other User", "email": "other-pr2@example.com",
              "handle": "otherpr2", "password": "pw123456"},
    )
    assert other.status_code == 201, other.text
    oh = {"Authorization": f"Bearer {other.json()['access_token']}"}
    me = client.get("/api/auth/me", headers=oh).json()
    secret_proj = client.post("/api/projects", json={"name": "Secret Unread"}, headers=oh).json()
    foreign = mem_svc.add_memory(
        db, text_body=secret_text, project_id=secret_proj["id"], status="published",
        origin="user:otherpr2", actor_user_id=me["id"],
        attributed_project_id=secret_proj["id"],
    )
    r = client.get(f"/api/memory/lessons/{shard.id}?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    cluster_ids = [c["id"] for c in body["cluster"]]
    assert foreign.id not in cluster_ids
    assert all(c.get("project_id") != secret_proj["id"] for c in body["cluster"])
    assert all(foreign.id != c["id"] for c in body["cluster"])
    assert "unread project" in body["unread_cluster_tags"]
    assert secret_proj["tag"] not in body["unread_cluster_tags"]
    assert body["eligibility"]["distinct_projects"] == 2


def test_mcp_pinned_key_does_not_leak_foreign_cluster_text(client, auth, db):
    p2 = client.post("/api/projects", json={"name": "Pinned Two"}, headers=auth).json()["id"]
    text = CLUSTER + " pinned key"
    a = mem_svc.add_memory(
        db, text_body=text, project_id="core", status="published",
        origin="user:ascme", actor_user_id="u1", attributed_project_id="core",
    )
    b = mem_svc.add_memory(
        db, text_body=text, project_id=p2, status="published",
        origin="user:ascme", actor_user_id="u1", attributed_project_id=p2,
    )
    key = _key(client, auth, project_id="core", scopes=["read", "write"])
    body = _mcp(client, key, "get_lessons", {"shard_id": a.id, "project_id": "core"})
    row = body["results"][0]
    cluster_ids = [c["id"] for c in row["cluster"]]
    assert b.id not in cluster_ids
    assert all(c.get("project_id") != p2 for c in row["cluster"])
    assert "unread project" in row["unread_cluster_tags"]
    assert row["eligibility"]["cluster_scan"] == "scanned"
    assert row["eligibility"]["distinct_projects"] == 2
    assert b.id not in cluster_ids


# ---- miss-on-publish --------------------------------------------------------------------


def test_set_status_publish_writes_missed_on_older(db):
    """Deleting maybe_record_recurrence_miss from set_status fails this."""
    older = mem_svc.add_memory(
        db, text_body="older published lesson miss-a", project_id="core",
        status="published", origin="user:ascme",
    )
    newer = mem_svc.add_memory(
        db, text_body="newer candidate lesson miss-a", project_id="core",
        status="candidate", origin="user:ascme", auto_triage=False,
    )
    _force_sim(db, older, newer, 0.90)
    assert older.scoring_source == ""
    mem_svc.set_status(db, newer.id, "published")
    kinds = [o.kind for o in _outcomes(db, older.id)]
    assert "missed" in kinds, kinds
    assert all(o.source == "recurrence" for o in _outcomes(db, older.id) if o.kind == "missed")


def test_republish_does_not_write_a_second_miss(db):
    """Hook fires on the transition, not on every POST publish of an already-published row."""
    older = mem_svc.add_memory(
        db, text_body="older published lesson miss-once", project_id="core",
        status="published", origin="user:ascme",
    )
    newer = mem_svc.add_memory(
        db, text_body="newer candidate lesson miss-once", project_id="core",
        status="candidate", origin="user:ascme", auto_triage=False,
    )
    _force_sim(db, older, newer, 0.90)
    mem_svc.set_status(db, newer.id, "published")
    assert len([o for o in _outcomes(db, older.id) if o.kind == "missed"]) == 1
    mem_svc.set_status(db, newer.id, "published")
    assert len([o for o in _outcomes(db, older.id) if o.kind == "missed"]) == 1


def test_agent_publish_keep_writes_missed_independently(db, monkeypatch):
    """Deleting the hook from agent_publish (leaving set_status) fails this."""
    older = mem_svc.add_memory(
        db, text_body="older published lesson miss-b", project_id="core",
        status="published", origin="user:ascme",
    )
    newer = mem_svc.add_memory(
        db, text_body="newer candidate lesson miss-b", project_id="core",
        status="candidate", origin="agent:k", auto_triage=False,
    )
    _force_sim(db, older, newer, 0.90)
    monkeypatch.setattr(
        mem_svc, "judge_verdict",
        lambda db, shard: ({"keep": True, "quality": 0.95, "reason": "ok"}, "ok"),
    )
    shard, verdict = mem_svc.agent_publish(db, newer, origin="agent:k")
    assert shard.status == "published"
    assert shard.scoring_source == "agent"
    kinds = [o.kind for o in _outcomes(db, older.id)]
    assert "missed" in kinds, kinds


def test_trusted_auto_publish_writes_zero_outcomes(client, auth, db):
    pid = client.post("/api/projects", json={"name": "Trusted Miss"}, headers=auth).json()["id"]
    client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "trusted"}, headers=auth)
    older = mem_svc.add_memory(
        db, text_body="older trusted pool", project_id=pid, status="published",
        origin="user:ascme",
    )
    newer = mem_svc.add_memory(
        db, text_body="newer trusted restatement", project_id=pid,
        status="candidate", origin="agent:k", auto_triage=False,
    )
    _force_sim(db, older, newer, 0.90)
    db.refresh(newer)
    published = mem_svc.triage_candidate(db, newer)
    assert published.status == "published"
    assert published.scoring_source == "trusted"
    assert _outcomes(db, older.id) == []


def test_similarity_auto_publish_writes_zero_outcomes(client, auth, db):
    pid = client.post("/api/projects", json={"name": "Auto Miss"}, headers=auth).json()["id"]
    client.patch(
        f"/api/projects/{pid}",
        json={"memory_write_mode": "auto", "memory_auto_reject": False},
        headers=auth,
    )
    older = mem_svc.add_memory(
        db, text_body="older auto pool", project_id=pid, status="published",
        origin="user:ascme",
    )
    newer = mem_svc.add_memory(
        db, text_body="newer auto restatement", project_id=pid,
        status="candidate", origin="agent:k", auto_triage=False,
    )
    _force_sim(db, older, newer, 0.92)
    db.refresh(newer)
    published = mem_svc.triage_candidate(db, newer)
    assert published.status == "published"
    assert published.scoring_source in ("similarity", "llm")
    assert _outcomes(db, older.id) == []


def test_ingest_point_nine_similar_writes_zero_outcomes(db, monkeypatch):
    from app.services.ingest import Event
    from app.services.ingest.runner import _record
    from tests.test_transcript_ingest import LESSON

    older = mem_svc.add_memory(
        db, text_body="older before ingest", project_id="core", status="published",
        origin="user:ascme",
    )
    a, b = _vec_pair(0.90)
    older.embedding = a
    db.commit()
    monkeypatch.setattr(mem_svc, "safe_embed", lambda text, *a, **k: b)
    ev = Event(
        session_id="sess-pr2-miss", harness="claude-code", project="core",
        ts="2026-08-29T00:00:00Z", kind="user", text=LESSON,
    )
    assert _record(db, mem_svc, ev, "core")
    assert _outcomes(db, older.id) == []


def test_near_dup_is_merge_not_miss(db):
    older = mem_svc.add_memory(
        db, text_body="dup older", project_id="core", status="published",
    )
    newer = mem_svc.add_memory(
        db, text_body="dup newer", project_id="core", status="candidate", auto_triage=False,
    )
    _force_sim(db, older, newer, 0.96)
    mem_svc.set_status(db, newer.id, "published")
    assert _outcomes(db, older.id) == []


# ---- attribution ------------------------------------------------------------------------


def test_ingest_then_human_publish_leaves_attributed_project_null(client, auth, db):
    from app.services.ingest import Event
    from app.services.ingest.runner import _record
    from tests.test_transcript_ingest import LESSON

    ev = Event(
        session_id="sess-pr2-attr", harness="claude-code", project="core",
        ts="2026-08-29T00:00:00Z", kind="user", text=LESSON,
    )
    assert _record(db, mem_svc, ev, "core")
    shard = db.scalars(
        select(MemoryShard).where(MemoryShard.source == "transcript:claude-code:sess-pr2-attr")
    ).first()
    assert shard is not None
    r = client.post(f"/api/memory/shards/{shard.id}/publish", headers=auth)
    assert r.status_code == 200, r.text
    db.refresh(shard)
    assert shard.attributed_project_id is None
    assert shard.actor_user_id == "u1"
    cluster = mem_svc.published_cluster(
        db, shard, readable_project_ids={"core"}, viewer_project_id="core",
    )
    elig = mem_svc.org_eligibility(shard, cluster["members"], scan=cluster["scan"])
    assert elig["state"] == "unverifiable"
    assert "distinct_projects" in elig["reason"]


def test_mcp_add_memory_stamps_attribution(client, auth, db):
    pid = client.post("/api/projects", json={"name": "MCP Attr"}, headers=auth).json()["id"]
    key = _key(client, auth, project_id=pid, scopes=["read", "write"])
    body = _mcp(client, key, "add_memory", {"text": "agent note with attribution", "project_id": pid})
    shard = db.get(MemoryShard, body["id"])
    assert shard.actor_user_id == "u1"
    assert shard.attributed_project_id == pid


# ---- evidence kind=lesson ---------------------------------------------------------------


def test_update_item_lesson_evidence_writes_applied(client, auth, db):
    shard = mem_svc.add_memory(
        db, text_body="applied lesson", project_id="core", status="published",
    )
    item = items_svc.create_item(db, project_id="core", title="cites a lesson")
    items_svc.update_item(db, item.id, evidence=[
        {"kind": "lesson", "shard_id": shard.id, "detail": "used it"},
    ])
    kinds = [o.kind for o in _outcomes(db, shard.id)]
    assert "applied" in kinds
    db.refresh(item)
    assert any(e.get("kind") == "lesson" and e.get("shard_id") == shard.id for e in item.evidence)
    stored = next(o for o in _outcomes(db, shard.id) if o.kind == "applied")
    assert stored.related_item_id == item.id
    r = client.get(f"/api/memory/lessons/{shard.id}?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    applied = next(o for o in r.json()["outcomes"] if o["kind"] == "applied")
    assert applied["related_item_id"] == item.key
    if item.id != item.key:
        assert applied["related_item_id"] != item.id


def test_gate_preview_does_not_write_applied_outcomes(db):
    shard = mem_svc.add_memory(
        db, text_body="preview must not apply", project_id="core", status="published",
    )
    items_svc.append_evidence([], [
        {"kind": "lesson", "shard_id": shard.id, "detail": "preview"},
    ])
    assert _outcomes(db, shard.id) == []


def test_incomplete_lesson_receipt_demotes_to_note(db):
    item = items_svc.create_item(db, project_id="core", title="bad lesson receipt")
    items_svc.update_item(db, item.id, evidence=[{"kind": "lesson", "detail": "no id"}])
    db.refresh(item)
    assert item.evidence
    assert item.evidence[0]["kind"] == "note"
    assert _outcomes(db, "missing") == []


def test_non_visible_lesson_demotes_and_writes_no_outcome(client, auth, db):
    other = client.post("/api/projects", json={"name": "Not Sibling Visible"}, headers=auth).json()["id"]
    shard = mem_svc.add_memory(
        db, text_body="project local of other", project_id=other, status="published",
    )
    item = items_svc.create_item(db, project_id="core", title="cannot see that lesson")
    items_svc.update_item(db, item.id, evidence=[
        {"kind": "lesson", "shard_id": shard.id, "detail": "nope"},
    ])
    db.refresh(item)
    assert item.evidence[0]["kind"] == "note"
    assert _outcomes(db, shard.id) == []


def test_org_reach_sibling_lesson_apply_works(client, auth, db):
    other = client.post("/api/projects", json={"name": "Apply Org"}, headers=auth).json()["id"]
    shard = mem_svc.add_memory(
        db, text_body="org apply", project_id=other, status="published",
    )
    shard.reach = "org"
    db.commit()
    item = items_svc.create_item(db, project_id="core", title="applies org lesson")
    items_svc.update_item(db, item.id, evidence=[
        {"kind": "lesson", "shard_id": shard.id, "detail": "used org lesson"},
    ])
    kinds = [o.kind for o in _outcomes(db, shard.id)]
    assert "applied" in kinds
