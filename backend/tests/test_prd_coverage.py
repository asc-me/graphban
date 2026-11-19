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
        "body": ("# Sync Spec\n\n## Ingest\nread the feed\n\n## Transform\nnormalize\n\n"
                 "## Acceptance criteria\n### Ingest\n- feed is read\n### Transform\n- rows normalized\n"),
    }, headers=auth).json()

    # Dry run proposes one task per section; nothing created yet.
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    assert [p["section"] for p in dry["proposals"]] == ["Ingest", "Transform"]
    assert dry["created"] == []

    # create=true creates linked backlog items.
    from tests.prd_approve import approve_id
    approve_id(prd["id"])
    made = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth).json()
    assert len(made["created"]) == 2
    db = SessionLocal()
    it = db.get(items_svc.Item, made["created"][0])
    assert it.prd_id == prd["id"] and it.prd_section == "Ingest"
    # No code graph here, so no touchpoints could be proposed: filed blocked, not backlog.
    assert it.status == "blocked" and "touchpoints" in it.blocker
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
    from tests.prd_approve import approve_id
    approve_id(prd["id"])
    made = _mcp(client, key, "decompose_prd", {"prd_id": prd["id"], "create": True})["structuredContent"]
    assert len(made["created"]) == 2
    cov = _mcp(client, key, "prd_coverage", {"prd_id": prd["id"]})["structuredContent"]
    assert cov["sections_with_tasks"] == 2 and cov["gaps"] == []
    assert cov["shaped"] is True
    assert cov["empty_sections"] == ["One", "Two"]


def test_mcp_prd_coverage_description_names_empty_vs_gaps():
    """THE CALL. Payload keys an agent never reads are a clean miss. The description
    is what tools/list ships."""
    from app.mcp_server import TOOLS
    desc = next(t["description"] for t in TOOLS if t["name"] == "prd_coverage")
    assert "empty_sections" in desc
    assert "shaped" in desc
    assert "gaps" in desc


def test_mcp_prd_coverage_names_empty_on_a_standard_template(client, auth):
    """REST coverage already returns empty_sections; MCP must not drop them."""
    key = client.post("/api/api-keys", json={"name": "cov-agent", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    prd = client.post("/api/prds", json={"title": "Fresh MCP", "project_id": "core"},
                      headers=auth).json()
    cov = _mcp(client, key, "prd_coverage", {"prd_id": prd["id"]})["structuredContent"]
    assert cov["shaped"] is True
    assert cov["empty_sections"]
    assert "Overview" in cov["empty_sections"]


def test_decompose_carries_per_item_acceptance_past_framing_budget(client, auth):
    from app.services.fleet import parse_acceptance
    from tests.prd_approve import approve_id

    big = "\n".join(f"- filler clause {i} " + "x" * 80 for i in range(120))
    body = (
        "# Spec\n\n## Context\nframing\n\n## Ingest\nread the feed\n\n"
        "## Transform\nnormalize\n\n## Export\nwrite out\n\n"
        f"## Acceptance criteria\n### Ingest\n- feed is read\n{big}\n"
        "### Transform\n- rows normalized\n### General\n"
        "- Orphan clause about nothing in particular\n"
    )
    prd = client.post("/api/prds", json={"title": "AC Spec", "body": body}, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert "Not carried" not in by["Ingest"]["description"]
    ingest, _ = parse_acceptance(by["Ingest"]["description"])
    assert ingest[0] == "feed is read" and "rows normalized" not in ingest
    assert parse_acceptance(by["Transform"]["description"])[0] == ["rows normalized"]
    assert dry["no_acceptance"] == ["Export"]
    assert dry["unassigned_clauses"] == ["Orphan clause about nothing in particular"]
    assert by["Export"]["blocked"] and "acceptance" in by["Export"]["blocker"]

    approve_id(prd["id"])
    made = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth).json()
    db = SessionLocal()
    statuses = {db.get(items_svc.Item, i).prd_section: db.get(items_svc.Item, i).status
                for i in made["created"]}
    db.close()
    assert statuses == {"Ingest": "blocked", "Transform": "blocked", "Export": "blocked"}  # no touchpoints


def test_decompose_proposes_touchpoints_from_code_graph(client, auth):
    from app.services import code_graph
    from tests.prd_approve import approve_id

    prd = client.post("/api/prds", json={
        "title": "Widgets Spec",
        "body": ("# Widgets Spec\n\n## Fix the parser\nchange backend/app/widgets.py to trim\n\n"
                 "## Elsewhere\nsomething nobody has code for\n\n"
                 "## Acceptance criteria\n### Fix the parser\n- trimmed\n### Elsewhere\n- done\n"),
    }, headers=auth).json()
    db = SessionLocal()
    pid = db.get(prd_svc.Prd, prd["id"]).project_id
    code_graph.describe_code(db, project_id=pid, nodes=[
        {"path": "backend/app/widgets.py", "kind": "file", "summary": "widgets"}])
    db.close()

    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert by["Fix the parser"]["touchpoints"] == ["backend/app/widgets.py"]
    assert by["Fix the parser"]["touchpoint_evidence"][0]["evidence"]
    assert by["Fix the parser"]["blocked"] is False
    assert dry["no_touchpoints"] == ["Elsewhere"]
    assert by["Elsewhere"]["blocked"] is True

    approve_id(prd["id"])
    made = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth).json()
    db = SessionLocal()
    its = {i.prd_section: i for i in (db.get(items_svc.Item, x) for x in made["created"])}
    assert its["Fix the parser"].touchpoints == ["backend/app/widgets.py"]
    assert its["Fix the parser"].status == "backlog"
    assert its["Elsewhere"].status == "blocked" and "touchpoints" in its["Elsewhere"].blocker
    db.close()


def test_decompose_flags_an_oversized_section_and_splits_three_ways(client, auth):
    """Three sub-headings, a clause under each: flagged, and create files three linked items.

    The pieces are inserted blocked and the split links are added before any of them
    becomes backlog, so a drain cannot claim one while the others are still unlinked.
    """
    from sqlalchemy import event, select

    from app.models import Item, Link
    from app.services.fleet import parse_acceptance
    from tests.prd_approve import approve_id

    body = (
        "# Spec\n\n## Small\none thing\n\n## Work\n"
        "### One\ndo the first\n### Two\ndo the second\n### Three\ndo the third\n\n"
        "## Acceptance criteria\n### Small\n- small is done\n"
        "### One\n- first is done\n### Two\n- second is done\n### Three\n- third is done\n"
    )
    prd = client.post("/api/prds", json={"title": "Split Spec", "body": body, "project_id": "core"},
                      headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert set(by) == {"Small", "Work"}
    assert by["Small"]["oversized"] is False
    assert by["Small"]["oversized_reason"] == ""
    assert by["Small"]["sub_items"] == []
    assert {s["signal"]: s["threshold"] for s in by["Small"]["size"]} == {
        "acceptance_clauses": 2, "touchpoint_areas": 2, "body_chars": 3000,
    }
    assert all(s["exceeded"] is False for s in by["Small"]["size"])

    work = by["Work"]
    assert work["oversized"] is True
    assert work["oversized_reason"] == "acceptance_clauses 3 > 2"
    clauses = next(s for s in work["size"] if s["signal"] == "acceptance_clauses")
    assert clauses["value"] == 3 and clauses["exceeded"] is True
    assert [s["heading"] for s in work["sub_items"]] == ["One", "Two", "Three"]
    assert [s["clauses"] for s in work["sub_items"]] == [
        ["first is done"], ["second is done"], ["third is done"],
    ]
    assert dry["unassigned_clauses"] == []
    assert "Work" not in dry["no_acceptance"]

    approve_id(prd["id"])
    seen: list[str] = []

    def _on_status(target, value, old, _initiator):
        if (target.title or "").startswith("Implement: Work —") and value == "backlog" and old == "blocked":
            seen.append("backlog")

    def _on_link(_target, _args, kwargs):
        if str(kwargs.get("reason", "")).startswith("split of "):
            seen.append("link")

    event.listen(Item.status, "set", _on_status)
    event.listen(Link, "init", _on_link)
    try:
        key = client.post("/api/api-keys", json={"name": "split-agent", "project_id": "core"},
                          headers=auth).json()["plaintext"]
        made = _mcp(client, key, "decompose_prd", {
            "prd_id": prd["id"], "create": True,
            "splits": {"Work": ["One", "Two", "Three"]},
            "touchpoints": {"Work": ["backend/app/work.py"], "Small": ["backend/app/small.py"]},
        })["structuredContent"]
    finally:
        event.remove(Item.status, "set", _on_status)
        event.remove(Link, "init", _on_link)

    assert seen == ["link", "link", "backlog", "backlog", "backlog"]
    assert len(made["created"]) == 4
    db = SessionLocal()
    items = [db.get(items_svc.Item, i) for i in made["created"]]
    small = next(i for i in items if i.prd_section == "Small")
    pieces = [i for i in items if i.prd_section == "Work"]
    assert small.status == "backlog" and small.touchpoints == ["backend/app/small.py"]
    assert len(pieces) == 3
    assert {i.status for i in pieces} == {"backlog"}
    assert {i.blocker for i in pieces} == {""}
    by_title = {i.title: i for i in pieces}
    assert parse_acceptance(by_title["Implement: Work — One"].description)[0] == ["first is done"]
    assert parse_acceptance(by_title["Implement: Work — Two"].description)[0] == ["second is done"]
    assert "first is done" not in parse_acceptance(by_title["Implement: Work — Three"].description)[0]
    links = list(db.scalars(select(Link).where(Link.reason == "split of Work")))
    assert len(links) == 2
    assert {lnk.type for lnk in links} == {"semantic"}
    ids = {i.id for i in pieces}
    assert {lnk.a for lnk in links} | {lnk.b for lnk in links} == ids
    db.close()

    again = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    assert again["proposals"] == []


def test_decompose_touchpoint_override(client, auth):
    from tests.prd_approve import approve_id

    prd = client.post("/api/prds", json={
        "title": "Ov Spec",
        "body": "# Ov\n\n## Thing\nbuild it\n\n## Acceptance criteria\n### Thing\n- built\n",
    }, headers=auth).json()
    approve_id(prd["id"])
    db = SessionLocal()
    out = prd_svc.decompose(db, prd_svc.get_prd(db, prd["id"]), create=True,
                            touchpoints={"Thing": ["a/b.py"]})
    it = db.get(items_svc.Item, out["created"][0])
    assert it.touchpoints == ["a/b.py"] and it.status == "backlog"
    assert out["no_touchpoints"] == []
    db.close()
