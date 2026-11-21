"""`search_items` can be limited to one PRD in SQL (GRPH-1645).

An idle drain that filtered in Python still loaded every item in the project. The
argument is fleet-only on the manifest; the handler resolves the caller's key to the
stored id and the query compares that column. Dropping `prd_id` on the handler
returns the other PRD's item.
"""
import json

from app.services import items as items_svc
from app.services import prds as prd_svc


def test_search_items_prd_id_returns_only_that_prd(client, auth):
    from app.db import SessionLocal

    with SessionLocal() as db:
        own = prd_svc.create_prd(db, title="Own", project_id="core", body="## Own\n")
        other = prd_svc.create_prd(db, title="Other", project_id="core", body="## Other\n")
        kept = items_svc.create_item(
            db, title="alpha-row", project_id="core", prd_id=own.id, status="next")
        other_item = items_svc.create_item(
            db, title="beta-row", project_id="core", prd_id=other.id, status="next")
        # The done gate is not what this test measures. The column is what the
        # SQL filter reads, and creating as done is refused without an attestation.
        kept.status = "done"
        other_item.status = "done"
        db.commit()
        found = items_svc.search_items(
            db, status="done", project_id="core", prd_id=own.id, limit=100)
        key = own.key

    assert [i.id for i in found] == [kept.id]

    minted = client.post(
        "/api/api-keys", json={"name": "prd-search", "project_id": "core"}, headers=auth,
    )
    assert minted.status_code == 201, minted.text
    token = minted.json()["plaintext"]
    reply = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "search_items", "arguments": {
                  "status": "done", "prd_id": key, "fields": "full", "limit": 100,
              }}},
        headers={"X-API-Key": token},
    ).json()["result"]
    assert reply.get("isError") is not True, reply
    body = json.loads(reply["content"][0]["text"])
    titles = [row["title"] for row in body["results"]]
    assert titles == ["alpha-row"]
    assert body["total"] == 1
