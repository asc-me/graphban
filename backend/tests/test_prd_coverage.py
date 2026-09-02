"""Feature D: spec-to-task traceability + PRD coverage."""
import json

from app.db import SessionLocal
from app.services import items as items_svc
from app.services import prds as prd_svc


def test_parse_sections_and_bodies(client):
    body = "# Title\n\n## Goals\n- ship it\n\n## Risks\nmaybe hard\n"
    assert prd_svc.parse_sections(body) == ["Goals", "Risks"]
    bodies = prd_svc.section_bodies(body)
    assert bodies["Goals"] == "- ship it" and bodies["Risks"] == "maybe hard"


def test_decompose_fills_gaps_and_links(client, auth):
    prd = client.post("/api/prds", json={
        "title": "Sync Spec",
        "body": "# Sync Spec\n\n## Ingest\nread the feed\n\n## Transform\nnormalize\n",
    }, headers=auth).json()

    # Dry run proposes one task per section; nothing created yet.
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    assert [p["section"] for p in dry["proposals"]] == ["Ingest", "Transform"]
    assert dry["created"] == []

    # create=true creates linked backlog items.
    made = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth).json()
    assert len(made["created"]) == 2
    db = SessionLocal()
    it = db.get(items_svc.Item, made["created"][0])
    assert it.prd_id == prd["id"] and it.prd_section == "Ingest" and it.status == "backlog"
    db.close()

    # Now those sections are covered — a second decompose finds no gaps.
    again = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    assert again["proposals"] == []


def test_standard_template_sections_are_empty_not_a_clean_pass(client, auth):
    """A fresh Standard PRD has headings, but only placeholders — empty, not occupied."""
    prd = client.post("/api/prds", json={"title": "Fresh"}, headers=auth).json()
    cov = client.get(f"/api/prds/{prd['id']}/coverage", headers=auth).json()
    assert cov["shaped"] is True
    assert set(cov["empty_sections"]) == {
        "Overview", "Goals", "Non-Goals", "Key Features",
        "Success Metrics", "Risks & Open Questions",
    }
    assert all(s["empty"] for s in cov["sections"])
    assert "percent_empty" not in cov and "occupancy" not in cov


def test_no_headings_is_unshaped_not_zero_empty(client, auth):
    """A body with no ## is not empty_sections=[] — that would read as a clean pass."""
    prd = client.post(
        "/api/prds",
        json={"title": "Blank", "body": "# Blank\n\njust prose, no sections\n"},
        headers=auth,
    ).json()
    cov = client.get(f"/api/prds/{prd['id']}/coverage", headers=auth).json()
    assert cov["shaped"] is False
    assert cov["empty_sections"] == []
    assert cov["section_count"] == 0


def test_occupied_section_is_not_empty(client, auth):
    prd = client.post(
        "/api/prds",
        json={"title": "Filled", "body": "# Filled\n\n## Goals\n- ship the ingest path\n"},
        headers=auth,
    ).json()
    cov = client.get(f"/api/prds/{prd['id']}/coverage", headers=auth).json()
    assert cov["shaped"] is True
    assert cov["empty_sections"] == []
    assert cov["sections"][0]["empty"] is False


def test_coverage_does_not_call_the_chat_model(client, auth, monkeypatch):
    """Occupancy is mechanical. Enriching GET /coverage with a judge would score every tab."""
    prd = client.post("/api/prds", json={"title": "NoChat"}, headers=auth).json()
    from app.services import platform as platform_svc
    calls = {"n": 0}
    real = platform_svc.resolve_chat

    def wrapped(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(platform_svc, "resolve_chat", wrapped)
    r = client.get(f"/api/prds/{prd['id']}/coverage", headers=auth)
    assert r.status_code == 200
    assert calls["n"] == 0


def test_coverage_rollup_and_gaps(client, auth):
    prd = client.post("/api/prds", json={
        "title": "Cov", "body": "# Cov\n\n## A\n\n## B\n\n## C\n",
    }, headers=auth).json()
    # Cover A (done) and B (in progress); leave C a gap.
    from tests.attest import complete_body
    a = client.post("/api/items", json={"title": "do A", "prd_id": prd["id"], "prd_section": "A"}, headers=auth).json()
    assert client.patch(f"/api/items/{a['id']}", json=complete_body(), headers=auth).status_code == 200
    client.post("/api/items", json={"title": "do B", "status": "in_progress", "prd_id": prd["id"], "prd_section": "B"}, headers=auth)

    cov = client.get(f"/api/prds/{prd['id']}/coverage", headers=auth).json()
    assert cov["section_count"] == 3 and cov["sections_with_tasks"] == 2
    assert cov["gaps"] == ["C"]
    assert cov["total_items"] == 2 and cov["done_items"] == 1 and cov["percent_done"] == 50
    by = {s["section"]: s for s in cov["sections"]}
    assert by["A"]["done"] == 1 and by["C"]["gap"] is True


def _mcp(client, key, name, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": args}}, headers={"X-API-Key": key})
    return r.json()["result"]


def test_mcp_decompose_and_coverage(client, auth):
    key = client.post("/api/api-keys", json={"name": "spec-agent", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    prd = client.post("/api/prds", json={
        "title": "MCP Spec", "body": "# MCP Spec\n\n## One\n\n## Two\n", "project_id": "core",
    }, headers=auth).json()
    made = _mcp(client, key, "decompose_prd", {"prd_id": prd["id"], "create": True})["structuredContent"]
    assert len(made["created"]) == 2
    cov = _mcp(client, key, "prd_coverage", {"prd_id": prd["id"]})["structuredContent"]
    assert cov["sections_with_tasks"] == 2 and cov["gaps"] == []
