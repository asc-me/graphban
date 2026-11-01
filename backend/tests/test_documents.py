"""GRPH-1275 — Document record: ledger rows, preamble, default index rule."""
from __future__ import annotations

import pytest

from app import errors
from app.mcp_server import LIVE_TOOL_COUNT, TOOLS
from app.models import Document
from app.services import documents as docs
from app.services import prds as prd_svc
from tests.prd_approve import approve

BODY = "## Tokens\nUse the teal accent everywhere.\n\n## Spacing\nEight point grid.\n"
KEY = "key:alex"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def pid(db):
    return db.scalars(__import__("sqlalchemy").select(__import__("app.models", fromlist=["Project"]).Project.id)).first()


def _baselined(db, pid, title="Visual system", body=BODY):
    prd = prd_svc.create_prd(db, title=title, project_id=pid, body=body)
    approve(db, prd)
    assert prd_svc.baseline_of(db, prd.id) is not None
    return prd


def test_tools_are_in_the_manifest():
    names = {t["name"] for t in TOOLS}
    assert {"put_document", "get_document", "search_docs"} <= names
    assert all("outputSchema" in t for t in TOOLS)
    assert LIVE_TOOL_COUNT == len(TOOLS)


def test_put_document_refuses_every_kind_but_preamble(db, pid):
    prd = _baselined(db, pid)
    for kind in ("design_prompt", "canvas", "set_constraints", "design_review"):
        with pytest.raises(errors.Validation):
            docs.put_document(db, project_id=pid, kind=kind, body="x", prd_id=prd.id, created_by=KEY)


def test_preamble_is_written_by_a_person(db, pid):
    prd = _baselined(db, pid)
    with pytest.raises(errors.Validation):
        docs.put_document(db, project_id=pid, kind="preamble", body="x", prd_id=prd.id,
                          created_by="GRPH-A1")


def test_preamble_cites_baseline_hash_and_versions(db, pid):
    prd = _baselined(db, pid)
    a = docs.put_document(db, project_id=pid, kind="preamble", body="Be calm.", prd_id=prd.id,
                          created_by=KEY)
    from app.hashing import content_hash
    assert a["prd_hash"] == content_hash(prd_svc.baseline_of(db, prd.id).body)
    assert a["version"] == 1 and a["chunks"][0]["extractor_version"] == docs.EXTRACTOR_VERSION
    same = docs.put_document(db, project_id=pid, kind="preamble", body="Be calm.",
                             prd_id=prd.id, created_by=KEY)
    assert same["id"] == a["id"]
    b = docs.put_document(db, project_id=pid, kind="preamble", body="Be bold.", prd_id=prd.id,
                          created_by=KEY)
    assert b["version"] == 2
    assert db.get(Document, a["id"]).governing is False
    assert db.get(Document, a["id"]).chunks[0].governing is False


def test_preamble_needs_a_baselined_prd(db, pid):
    prd = prd_svc.create_prd(db, title="Draft", project_id=pid, body=BODY)
    with pytest.raises(errors.Validation):
        docs.put_document(db, project_id=pid, kind="preamble", body="x", prd_id=prd.id,
                          created_by=KEY)


def test_get_document_omits_blob_key_and_404s(db, pid):
    prd = _baselined(db, pid)
    d = docs.put_document(db, project_id=pid, kind="preamble", body="x", prd_id=prd.id,
                          created_by=KEY)
    assert "blob_key" not in docs.get_document(db, pid, d["id"])
    with pytest.raises(errors.NotFound):
        docs.get_document(db, pid, "DOC-missing")


def test_search_defaults_to_baseline_and_says_drafts_excluded(db, pid):
    base = _baselined(db, pid)
    draft = prd_svc.create_prd(db, title="Draft", project_id=pid,
                               body="## Secret\nzebra stripes\n")
    r = docs.search_docs(db, pid, "teal")
    assert [h["prd_id"] for h in r["results"]] == [base.id]
    from app.hashing import content_hash
    assert r["results"][0]["content_hash"] == content_hash(prd_svc.baseline_of(db, base.id).body)
    r = docs.search_docs(db, pid, "zebra")
    assert r["results"] == [] and "draft PRDs were excluded" in r["note"]
    r = docs.search_docs(db, pid, "zebra", include_drafts=True)
    assert r["results"][0]["prd_id"] == draft.id and r["results"][0]["status"] == "draft"


def test_baseline_text_stays_indexed_after_live_edit(db, pid):
    prd = _baselined(db, pid)
    prd.body = "## Tokens\nviolet only\n"
    db.commit()
    assert docs.search_docs(db, pid, "violet")["results"] == []
    assert docs.search_docs(db, pid, "teal")["results"]


def test_preamble_not_indexed_by_default(db, pid):
    prd = _baselined(db, pid)
    docs.put_document(db, project_id=pid, kind="preamble", body="quokka rules", prd_id=prd.id,
                      created_by=KEY)
    assert docs.search_docs(db, pid, "quokka")["results"] == []
    assert docs.search_docs(db, pid, "quokka", include_design=True)["results"]


def test_unaccepted_prompt_and_canvas_never_indexed(db, pid):
    prd = _baselined(db, pid)
    for kind, accepted in (("design_prompt", False), ("design_prompt", True), ("canvas", True)):
        d = Document(id=docs.new_id(), project_id=pid, kind=kind, prd_id=prd.id,
                     content_hash="sha256:x", accepted=accepted, screen=f"{kind}{accepted}")
        docs._chunk(d, "## H\nokapi")
        db.add(d)
    db.commit()
    r = docs.search_docs(db, pid, "okapi", include_design=True)
    assert [h["kind"] for h in r["results"]] == ["design_prompt"]
    assert docs.search_docs(db, pid, "okapi")["results"] == []


def test_stale_chunks_carry_reason_only_with_include_design(db, pid):
    prd = _baselined(db, pid)
    d = Document(id=docs.new_id(), project_id=pid, kind="design_prompt", prd_id=prd.id,
                 content_hash="sha256:x", accepted=True, status="stale", stale_reason="preamble")
    docs._chunk(d, "## H\nokapi")
    db.add(d)
    db.commit()
    assert docs.search_docs(db, pid, "okapi")["results"] == []
    hit = docs.search_docs(db, pid, "okapi", include_design=True)["results"][0]
    assert hit["status"] == "stale" and hit["stale_reason"] == "preamble"


def test_old_extractor_chunks_stay_until_superseded(db, pid):
    prd = _baselined(db, pid)
    d = docs.put_document(db, project_id=pid, kind="preamble", body="x", prd_id=prd.id,
                          created_by=KEY)
    row = db.get(Document, d["id"])
    row.chunks[0].extractor_version = "0"
    db.commit()
    assert docs.get_document(db, pid, d["id"])["chunks"][0]["extractor_version"] == "0"


def _mcp(client, headers, name, arguments, call_id=1):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers=headers,
    )


def test_mcp_round_trip(client, auth, db, pid):
    prd = _baselined(db, pid)
    key = client.post("/api/api-keys", json={"name": "docs-e2e"}, headers=auth).json()["plaintext"]
    h = {"X-API-Key": key}
    put = _mcp(client, h, "put_document", {
        "kind": "preamble", "prd_id": prd.id, "body": "## Voice\nquokka calm", "project_id": pid})
    assert "error" not in put.json(), put.text
    doc = put.json()["result"]["structuredContent"]
    got = _mcp(client, h, "get_document", {"id": doc["id"], "project_id": pid}, 2)
    assert got.json()["result"]["structuredContent"]["id"] == doc["id"]
    srch = _mcp(client, h, "search_docs", {"query": "teal", "project_id": pid}, 3)
    assert srch.json()["result"]["structuredContent"]["results"][0]["prd_id"] == prd.id


def test_mcp_put_document_refuses_a_session_seat_without_agent_id(client, auth, db, pid):
    """A registered seat that leaves out `agent_id` is still a seat: the session resolves it,
    so the preamble's person rule cannot be bypassed by omission (GRPH-1275 bounce)."""
    import json

    prd = _baselined(db, pid)
    key = client.post("/api/api-keys", json={"name": "docs-seat", "project_id": pid},
                      headers=auth).json()["plaintext"]
    init = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {}}, headers={"X-API-Key": key})
    h = {"X-API-Key": key, "Mcp-Session-Id": init.headers["mcp-session-id"]}
    # No seat: an all-in-one agent, which the role gate lets through without `agent_id`.
    reg = _mcp(client, h, "register_agent", {"label": "P"})
    assert json.loads(reg.json()["result"]["content"][0]["text"])["agent_id"]

    put = _mcp(client, h, "put_document", {
        "kind": "preamble", "prd_id": prd.id, "body": "## Voice\ncalm", "project_id": pid}, 2)
    body = put.json()
    assert "error" in body or body["result"].get("isError"), put.text
    assert "person" in put.text
    assert not db.scalars(__import__("sqlalchemy").select(Document).where(
        Document.project_id == pid, Document.kind == "preamble")).first()


def test_mcp_put_document_refuses_any_named_agent_id(client, auth, db, pid):
    prd = _baselined(db, pid)
    key = client.post("/api/api-keys", json={"name": "docs-named"}, headers=auth).json()["plaintext"]
    put = _mcp(client, {"X-API-Key": key}, "put_document", {
        "kind": "preamble", "prd_id": prd.id, "body": "x", "project_id": pid,
        "agent_id": "key:docs-named"})
    assert "person" in put.text
