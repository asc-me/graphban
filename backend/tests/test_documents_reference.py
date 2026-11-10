"""GRPH-1495 — `reference` documents: non-governing, agent-writable, versioned, linked."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import errors
from app.models import Document, Project
from app.services import documents as docs
from app.services import items as items_svc
from app.services import prds as prd_svc
from sqlalchemy import select

AGENT = "GRPH-A9"
RULES = (Path(__file__).resolve().parents[2] / "docs" / "rule-candidates.md").read_text()


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
    return db.scalars(select(Project.id)).first()


def _ref(db, pid, body, title="Rule candidates", **kw):
    return docs.put_document(db, project_id=pid, kind="reference", body=body, title=title,
                             prd_id=kw.pop("prd_id", None), created_by=kw.pop("by", AGENT), **kw)


def test_agent_writes_and_versions_a_reference(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body="## A\nx\n")
    a = _ref(db, pid, "## One\nfirst", prd_id=prd.id)
    assert a["id"] and a["version"] == 1 and a["governing"] is False
    b = _ref(db, pid, "## One\nsecond", prd_id=prd.id)
    assert b["version"] == 2 and b["id"] != a["id"]
    old = docs.get_document(db, pid, a["id"])
    assert old["version"] == 1 and "first" in old["chunks"][0]["text"]
    assert _ref(db, pid, "## One\nsecond", prd_id=prd.id)["id"] == b["id"]  # unchanged: no new row


def test_agent_cannot_write_a_preamble(db, pid):
    with pytest.raises(errors.Validation, match="person"):
        docs.put_document(db, project_id=pid, kind="preamble", body="x", prd_id=None,
                          created_by=AGENT)
    assert not db.scalars(select(Document).where(
        Document.project_id == pid, Document.kind == "preamble")).first()


def test_default_search_excludes_reference_and_says_so(db, pid):
    _ref(db, pid, RULES)
    assert not any(d.governing for d in db.scalars(
        select(Document).where(Document.kind == "reference")))
    q = "quarantine after three refusals"
    default = docs.search_docs(db, pid, q)
    assert not [h for h in default["results"] if h["kind"] == "reference"]
    assert "reference" in default["reference_note"]
    opted = docs.search_docs(db, pid, q, include_reference=True)
    hits = [h for h in opted["results"] if h["kind"] == "reference"]
    assert hits and "reference_note" not in opted


def test_links_listed_without_touching_the_prd_body(db, pid):
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body="## A\nx\n")
    item = items_svc.create_item(db, title="T", project_id=pid)
    before = prd_svc.body_hash(prd.body)
    r1 = _ref(db, pid, "## a\nb", prd_id=prd.id)
    r2 = _ref(db, pid, "## a\nc", title="Other", item_id=item.id)
    assert prd_svc.body_hash(prd_svc.get_prd(db, prd.id).body) == before
    assert [r["id"] for r in docs.references_for(db, prd_id=prd.id)] == [r1["id"]]
    details = items_svc.get_item_details(db, item.id)
    assert details["reference_documents"] == [
        {"id": r2["id"], "title": "Other", "version": 1}]


def test_missing_links_are_refused(db, pid):
    with pytest.raises(errors.Validation):
        _ref(db, pid, "x", prd_id="PRD-nope")
    with pytest.raises(errors.Validation):
        _ref(db, pid, "x", item_id="NOPE-1")


def test_mcp_agent_key_round_trip(client, auth, db, pid):
    key = client.post("/api/api-keys", json={"name": "ref-e2e"}, headers=auth).json()["plaintext"]
    h = {"X-API-Key": key}
    prd = prd_svc.create_prd(db, title="P", project_id=pid, body="## A\nx\n")

    def call(name, args, i=1):
        return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": i, "method": "tools/call",
                           "params": {"name": name, "arguments": {**args, "project_id": pid}}},
                           headers=h).json()

    put = call("put_document", {"kind": "reference", "title": "R", "body": "## s\nquokka",
                                "prd_id": prd.id})
    doc = put["result"]["structuredContent"]
    assert doc["version"] == 1
    got = call("get_prd", {"prd_id": prd.id}, 2)["result"]["structuredContent"]
    assert got["reference_documents"][0]["id"] == doc["id"]
    bad = call("put_document", {"kind": "reference", "title": "R", "body": "x",
                                "item_id": "NOPE-1"}, 3)
    assert "error" in bad or bad["result"].get("isError")
    pre = call("put_document", {"kind": "preamble", "body": "x", "agent_id": "GRPH-A1"}, 4)
    assert "person" in str(pre)
