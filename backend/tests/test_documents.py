"""GRPH-1275 — Document record: ledger rows, preamble, default index rule."""
from __future__ import annotations

import pytest

from app import errors
from app.mcp_server import LIVE_TOOL_COUNT, TOOLS
from sqlalchemy import select

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


def test_preamble_is_never_searched_or_embedded(db, pid, monkeypatch):
    from app.config import settings
    import app.embeddings as emb
    prd = _baselined(db, pid)
    monkeypatch.setattr(settings, "embed_provider", "openai")
    monkeypatch.setattr(emb, "safe_embed", lambda t: _vec())
    d = docs.put_document(db, project_id=pid, kind="preamble", body="quokka rules",
                          prd_id=prd.id, created_by=KEY)
    row = db.get(Document, d["id"])
    assert row.chunks and all(c.embedding is None for c in row.chunks)
    for flags in ({}, {"include_design": True}, {"include_design": True, "include_drafts": True}):
        assert not [h for h in docs.search_docs(db, pid, "quokka", **flags)["results"]
                    if h["kind"] == "preamble"]
    docs.backfill_embeddings(db)
    assert all(c.embedding is None for c in db.get(Document, d["id"]).chunks)


def test_set_constraints_never_searched(db, pid):
    prd = _baselined(db, pid)
    d = Document(id=docs.new_id(), project_id=pid, kind="set_constraints", prd_id=prd.id,
                 content_hash="sha256:x", accepted=True)
    docs._chunk(d, "## H\nokapi")
    db.add(d)
    db.commit()
    assert docs.search_docs(db, pid, "okapi", include_design=True,
                            include_drafts=True)["results"] == []


def test_prompt_is_embedded_only_at_acceptance(db, pid, monkeypatch):
    from app.config import settings
    import app.embeddings as emb
    prd = _baselined(db, pid)
    monkeypatch.setattr(settings, "embed_provider", "openai")
    monkeypatch.setattr(emb, "safe_embed", lambda t: _vec())
    d = Document(id=docs.new_id(), project_id=pid, kind="design_prompt", prd_id=prd.id,
                 content_hash="sha256:x", accepted=False)
    docs._chunk(d, "## H\nokapi")
    assert docs.embed_document(d) == 0 and all(c.embedding is None for c in d.chunks)
    d.accepted = True
    assert docs.embed_document(d) == 1 and d.chunks[0].embedding == _vec()


def test_search_does_not_write_an_unindexed_baseline(db, pid):
    from app.models import DocumentChunk
    prd = _baselined(db, pid)
    docs.drop_prd_chunks(db, prd.id)  # as if frozen before the index existed
    r = docs.search_docs(db, pid, "teal")
    assert r["results"][0]["source_id"] == prd.id and r["results"][0]["status"] == "baseline"
    assert r["ranking"] == "lexical"
    assert not list(db.scalars(select(DocumentChunk).where(DocumentChunk.prd_id == prd.id)))
    docs.backfill_embeddings(db)
    assert list(db.scalars(select(DocumentChunk).where(DocumentChunk.prd_id == prd.id)))


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


def _vec():
    from app.config import settings
    return [1.0] + [0.0] * (int(settings.embed_dim) - 1)


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


# ---- GRPH-1276: blob storage ----

class _Boom:
    name = "boom-s3"

    def put_bytes(self, key, data):
        raise OSError("connection refused")


def test_blob_put_stores_bytes_at_the_hash_and_shares_objects(db, pid, tmp_path):
    import hashlib

    from app.services.drive_sync import LocalFolderBackend

    be = LocalFolderBackend(tmp_path)
    data = b"\x89PNG canvas"
    a = docs.put_blob(db, project_id=pid, kind="canvas", data=data, mime="text/html",
                      screen="a", backend=be)
    b = docs.put_blob(db, project_id=pid, kind="canvas", data=data, mime="text/html",
                      screen="b", backend=be)
    digest = hashlib.sha256(data).hexdigest()
    assert a["blob_status"] == b["blob_status"] == "stored"
    assert a["content_hash"] == digest and a["blob_size"] == len(data)
    assert len(list((tmp_path / "blobs").rglob(digest))) == 1
    got, mime = docs.read_blob(db, pid, a["id"], backend=be)
    assert got == data and mime == "text/html"


def test_backend_failure_keeps_a_pending_row_and_retry_stores_same_hash(db, pid, tmp_path):
    from app.services.drive_sync import LocalFolderBackend

    data = b"bytes"
    with pytest.raises(errors.Unavailable) as ei:
        docs.put_blob(db, project_id=pid, kind="canvas", data=data, mime="x/y",
                      screen="s", backend=_Boom())
    assert "boom-s3" in str(ei.value)
    row = db.query(Document).filter_by(project_id=pid, kind="canvas").one()
    assert row.blob_status == "pending"
    d = docs.get_document(db, pid, row.id)
    assert d["blob_status"] == "pending" and "blob_key" not in d
    with pytest.raises(errors.BlobPending) as bi:
        docs.read_blob(db, pid, row.id, backend=LocalFolderBackend(tmp_path))
    assert row.content_hash in bi.value.hint
    be = LocalFolderBackend(tmp_path)
    again = docs.put_blob(db, project_id=pid, kind="canvas", data=data, mime="x/y",
                          screen="s", backend=be)
    assert again["id"] == row.id and again["blob_status"] == "stored"
    assert "blob_key" not in docs.get_document(db, pid, row.id)
    assert docs.read_blob(db, pid, row.id, backend=be)[0] == data
    with pytest.raises(errors.NotFound):
        docs.read_blob(db, pid, "DOC-missing", backend=be)


def test_superseded_blob_keeps_its_object(db, pid, tmp_path):
    from app.services.drive_sync import LocalFolderBackend

    be = LocalFolderBackend(tmp_path)
    one = docs.put_blob(db, project_id=pid, kind="canvas", data=b"1", mime="m", screen="s", backend=be)
    two = docs.put_blob(db, project_id=pid, kind="canvas", data=b"2", mime="m", screen="s", backend=be)
    assert two["version"] == 2 and two["governing"]
    assert docs.read_blob(db, pid, one["id"], backend=be)[0] == b"1"


def test_bytes_route_is_409_blob_pending(client, auth, db, pid):
    row = Document(id="DOC-pend", project_id=pid, kind="canvas", content_hash="ab" * 32,
                   blob_status="pending")
    db.add(row)
    db.commit()
    r = client.get("/api/documents/DOC-pend/bytes", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "blob_pending"
    assert client.get("/api/documents/DOC-nope/bytes", headers=auth).status_code == 404


def test_baseline_freeze_writes_prd_chunks_with_null_vector_under_stub(db, pid):
    from app.models import DocumentChunk
    from app.hashing import content_hash
    prd = _baselined(db, pid)
    rows = list(db.scalars(select(DocumentChunk).where(DocumentChunk.prd_id == prd.id)))
    assert rows and all(r.kind == "prd" and r.document_id is None and r.embedding is None
                        for r in rows)
    assert {r.content_hash for r in rows} == {content_hash(prd_svc.baseline_of(db, prd.id).body)}
    r = docs.search_docs(db, pid, "teal")
    assert r["ranking"] == "lexical" and "lexical" in r["ranking_note"]
    assert r["results"][0]["source_id"] == prd.id and r["results"][0]["kind"] == "prd"


def test_long_section_splits_on_paragraphs_and_cites_one_heading(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "embed_max_chars", 30)
    parts = docs.extract("## Big\n" + "\n\n".join(["a" * 20, "b" * 20, "c" * 20]))
    assert [h for h, _ in parts] == ["Big"] * 3 and all(len(t) <= 30 for _, t in parts)


def test_semantic_ranking_when_vectors_exist(db, pid, monkeypatch):
    from app.config import settings
    from app.models import DocumentChunk
    import app.embeddings as emb
    prd = _baselined(db, pid)
    for c in db.scalars(select(DocumentChunk).where(DocumentChunk.prd_id == prd.id)):
        c.embedding = _vec()
    db.commit()
    monkeypatch.setattr(settings, "embed_provider", "openai")
    monkeypatch.setattr(emb, "safe_embed", lambda t: _vec())
    r = docs.search_docs(db, pid, "anything at all", top_k=1)
    assert r["ranking"] == "semantic" and len(r["results"]) == 1 and "ranking_note" not in r


def test_superseded_chunks_only_with_include_drafts(db, pid):
    from app.models import DocumentChunk
    prd = _baselined(db, pid)
    for c in db.scalars(select(DocumentChunk).where(DocumentChunk.prd_id == prd.id)):
        c.governing = False
    db.commit()
    r = docs.search_docs(db, pid, "teal", include_drafts=True)
    assert any(h["status"] == "superseded" for h in r["results"])
