"""Phase 1: real memory intelligence + full MCP surface (all on the stub provider)."""
import json
from app.services import items as items_svc
from tests import attest


def _key(client, auth):
    return client.post("/api/api-keys", json={"name": "p1"}, headers=auth).json()["plaintext"]


def _call(client, key, name, args):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": key},
    )
    return json.loads(r.json()["result"]["content"][0]["text"])


def test_reembed_on_shard_edit_changes_ranking(client, auth):
    # Create a shard about "kubernetes", then edit it to be about "postgres".
    s = client.post(
        "/api/memory/shards", json={"text": "Runs on kubernetes clusters", "scope": "global", "project_id": "core"}, headers=auth
    ).json()
    client.patch(f"/api/memory/shards/{s['id']}", json={"text": "Runs on a single postgres container"}, headers=auth)
    hits = client.post(
        "/api/memory/search", json={"query": "postgres container", "top_k": 1, "project_id": "core"}, headers=auth
    ).json()
    assert "postgres" in hits[0]["shard"]["text"]  # re-embedded, so it now matches


def test_auto_extraction_on_done(client, auth):
    before = len(client.get("/api/memory/shards?project_id=core", headers=auth).json())
    # AL-15 is `next` in the seed; moving it to done should mint a lesson shard.
    client.patch("/api/items/AL-15", json=attest.complete_body(), headers=auth)
    shards = client.get("/api/memory/shards?project_id=core", headers=auth).json()
    assert len(shards) == before + 1
    assert any(s["source"] == "lesson from AL-15" for s in shards)
    # Idempotent: re-setting done doesn't double-extract.
    client.patch("/api/items/AL-15", json={"status": "review"}, headers=auth)
    client.patch("/api/items/AL-15", json=attest.complete_body(), headers=auth)
    assert len(client.get("/api/memory/shards?project_id=core", headers=auth).json()) == before + 1


def test_export_then_import_roundtrip(client, auth):
    # project_id is required since the authz pass (AL-42) — no all-projects dump.
    exported = client.get("/api/memory/export?project_id=core", headers=auth).json()["shards"]
    assert len(exported) == 5
    n = client.post("/api/memory/import", json={"shards": exported[:2]}, headers=auth).json()["imported"]
    assert n == 2
    assert len(client.get("/api/memory/shards?project_id=core", headers=auth).json()) == 7


def test_backfill_reembeds_all(client, auth):
    r = client.post("/api/memory/backfill", headers=auth).json()
    assert r["reembedded"] == 5
    assert "code_reembedded" in r  # backfill now covers code nodes too (AL-64)


def test_agent_chat_grounded(client, auth):
    r = client.post("/api/agent/chat", json={"message": "pgvector self-host"}, headers=auth).json()
    assert "Project state" in r["reply"]
    assert len(r["shards"]) >= 1


def test_agent_chat_stream_sse(client, auth):
    r = client.post("/api/agent/chat/stream", json={"message": "pgvector self-host"}, headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: shards" in body
    assert "event: delta" in body
    assert body.rstrip().endswith("event: done\ndata: {}")
    # Reassemble the streamed reply from the delta events.
    reply = ""
    for block in body.split("\n\n"):
        if block.startswith("event: delta"):
            data = block.split("data: ", 1)[1]
            reply += json.loads(data)["text"]
    assert "Project state" in reply


def test_mcp_all_new_tools(client, auth):
    key = _key(client, auth)

    backlog = _call(client, key, "get_backlog", {"limit": 5})
    assert all(i["status"] in ("backlog", "next") for i in backlog["results"])
    assert backlog["limit"] == 5 and "total" in backlog and "has_more" in backlog

    details = _call(client, key, "get_item_details", {"id": "AL-08"})
    # Looked up by its stored id, answered with its RENDERED key — the same shape
    # search_items and /api/items return. See test_retag_read_surfaces.py.
    assert details["id"] == "CP-8", details["id"]
    assert "linked_shards" in details

    nxt = _call(client, key, "suggest_next", {})
    assert nxt["item"]["status"] in ("next", "backlog")

    link = _call(client, key, "link_items", {"a": "AL-12", "b": "AL-08", "type": "dependency"})
    # Link endpoints render like every other reference (PRD-13); AL-12 is the frozen id.
    assert link["a"] == "CP-12" and link["type"] == "dependency"

    lessons = _call(client, key, "extract_lessons", {"id": "AL-11"})
    assert lessons.get("scheduled") is True
    assert lessons["results"] == []
    details = _call(client, key, "get_item_details", {"id": "AL-11"})
    assert details.get("linked_shards"), "background extraction should link shards to the item"

    digest = _call(client, key, "generate_digest", {})
    # Decision-packet shape (AL-52): state + the five decision-ready sections.
    assert "digest" in digest
    text = digest["digest"]
    assert "**State**" in text
    assert "**Smallest unresolved choice**" in text


def test_digest_is_a_decision_packet(client, auth):
    """AL-52: the digest leads with state and ends on the smallest unresolved choice;
    an in-review item is the choice to make."""
    pid = client.post("/api/projects", json={"name": "Packet"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "d", "project_id": pid}, headers=auth).json()["plaintext"]
    rev = _call(client, key, "create_item", {"title": "Ship the API"})["id"]
    _call(client, key, "update_item", {"id": rev, "status": "review"})
    wip = _call(client, key, "create_item", {"title": "Fix the parser"})["id"]
    _call(client, key, "update_item", {"id": wip, "status": "in_progress"})

    text = _call(client, key, "generate_digest", {})["digest"]
    for section in ("**State**", "**Attempted**", "**Evidence**", "**Risk**", "**Smallest unresolved choice**"):
        assert section in text, f"missing section {section}"
    assert wip in text.split("**Evidence**")[0]  # in-flight item under Attempted
    choice = text.split("**Smallest unresolved choice**")[1]
    assert rev in choice and "send back" in choice  # a review item is the decision


def test_proof_on_done_records_evidence_and_audit(client, auth):
    """AL-53: update_item accepts proof receipts (normalized), and the proof rides into
    the audit ledger so a completion is auditable against its evidence."""
    pid = client.post("/api/projects", json={"name": "Proof"}, headers=auth).json()["id"]
    # `gate` because completing now needs an `attestation`, and only that scope may write one
    # (GRPH-541/543). The receipts below are still the point of this test.
    key = client.post("/api/api-keys",
                      json={"name": "pf", "project_id": pid,
                            "scopes": ["read", "write", "gate"]},
                      headers=auth).json()["plaintext"]
    it = _call(client, key, "create_item", {"title": "Ship parser"})
    updated = _call(client, key, "update_item", {
        "id": it["id"], "status": "done",
        "evidence": [
            {"kind": "test", "detail": "142 passed", "url": "https://ci/run/9"},
            {"kind": "bogus", "detail": "looks fine"},  # unknown kind → note
            {"detail": "", "url": ""},                   # no detail/url → dropped
            attest.attestation(),
        ],
    })
    assert updated["status"] == "done"
    # The attestation the gate required is trailing; this test is about the three receipts
    # in front of it — normalised, demoted, and dropped respectively.
    assert updated["evidence"][:2] == [
        {"kind": "test", "detail": "142 passed", "url": "https://ci/run/9"},
        {"kind": "note", "detail": "looks fine", "url": ""},
    ]
    assert [e["kind"] for e in updated["evidence"]] == ["test", "note", "attestation"], \
        "the empty receipt must still be dropped, and nothing else added"
    events = client.get("/api/events", headers=auth).json()["results"]
    ev = next(e for e in events if e["action"] == "update_item" and e["target_id"] == it["id"])
    assert ev["meta"]["evidence"][0]["detail"] == "142 passed"


def test_proof_on_done_via_rest(client, auth):
    it = client.post("/api/items", json={"title": "Do X", "project_id": "core"}, headers=auth).json()
    up = client.patch(f"/api/items/{it['id']}",
                      json=attest.complete_body(evidence=[{"kind": "health", "detail": "prod green"}]),
                      headers=auth).json()
    assert up["evidence"][0] == {"kind": "health", "detail": "prod green", "url": ""}, \
        "the caller's own receipt must survive, and come first"
    assert items_svc.has_valid_attestation(up["evidence"]), \
        "the completion the gate allowed must be the one recorded"


def test_link_items_rejects_bad_type(client, auth):
    key = _key(client, auth)
    out = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "link_items", "arguments": {"a": "AL-1", "b": "AL-2", "type": "banana"}}},
        headers={"X-API-Key": key},
    ).json()
    assert out["result"]["isError"] is True
