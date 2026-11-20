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


def test_named_design_pages_become_a_design_block_and_a_ui_item(client, auth):
    """A section that names two design pages files an item holding both, plus a UI item."""
    from sqlalchemy import select

    from app.models import Link
    from tests.prd_approve import approve_id

    prd = client.post("/api/prds", json={
        "title": "Line Spec",
        "body": (
            "# Line Spec\n\n## The line\nList unscoped fixes.\n\n"
            "Design: https://claude.ai/artifact/abc\n"
            "Pages: Fix Dialog, Tracker - File a Fix\n\n"
            "## Acceptance criteria\n### The line\n- unscoped fixes are listed\n"
        ),
    }, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    described = by["The line"]["description"]
    assert "## Design" in described
    assert "https://claude.ai/artifact/abc" in described
    assert "Fix Dialog" in described and "Tracker - File a Fix" in described
    assert len(dry["ui_items"]) == 1
    ui = dry["ui_items"][0]
    assert ui["section"] == "The line" and ui["depends_on"] == "The line"
    assert ui["touchpoints"] == ["web/"]
    assert "Fix Dialog" in ui["description"] and "Tracker - File a Fix" in ui["description"]
    assert dry["ui_without_design"] == [] and dry["ui_skipped"] == []

    approve_id(prd["id"])
    made = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth).json()
    assert len(made["created"]) == 2
    db = SessionLocal()
    items = {db.get(items_svc.Item, i).title: db.get(items_svc.Item, i) for i in made["created"]}
    behaviour, ui_item = items["Implement: The line"], items["Implement: The line UI"]
    assert "Fix Dialog" in behaviour.description and "Tracker - File a Fix" in behaviour.description
    assert ui_item.touchpoints == ["web/"] and ui_item.prd_section == "The line"
    assert ui_item.status == "backlog"
    link = db.scalars(select(Link).where(Link.a == ui_item.id, Link.b == behaviour.id)).first()
    assert link is not None and link.type == "dependency"
    db.close()


def test_ui_mention_without_a_design_page_is_listed(client, auth):
    prd = client.post("/api/prds", json={
        "title": "Bare UI",
        "body": "# Bare\n\n## The screen\nThe UI shows the count.\n\n## The line\nStore the count.\n",
    }, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    assert dry["ui_without_design"] == ["The screen"]
    assert dry["ui_items"] == []


def test_web_only_section_names_pages_and_says_why_it_did_not_split(client, auth):
    prd = client.post("/api/prds", json={
        "title": "Web only",
        "body": (
            "# Web\n\n## The line\nDraw the row.\n\n"
            "Design: https://claude.ai/artifact/abc\n"
            "Pages: Fix Dialog, Tracker - File a Fix\n\n"
            "## Acceptance criteria\n### The line\n- the row is drawn\n"
        ),
    }, headers=auth).json()
    db = SessionLocal()
    out = prd_svc.decompose(
        db, prd_svc.get_prd(db, prd["id"]),
        touchpoints={"The line": ["web/src/Line.tsx"]})
    db.close()
    assert out["ui_items"] == []
    assert out["ui_skipped"] == [{
        "section": "The line",
        "why": prd_svc._UI_ALREADY_WEB,
    }]
    assert out["proposals"][0]["touchpoints"] == ["web/src/Line.tsx"]
    assert "Fix Dialog" in out["proposals"][0]["description"]
    assert "Tracker - File a Fix" in out["proposals"][0]["description"]


def test_a_framed_section_with_design_pages_says_why_no_ui_item(client, auth):
    prd = client.post("/api/prds", json={
        "title": "Framed",
        "body": (
            "# Framed\n\n## Goals\nShip the line.\n\n"
            "Design: https://claude.ai/artifact/abc\n"
            "Pages: Home\n\n"
            "## The line\nStore the count.\n"
        ),
    }, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    assert dry["ui_items"] == []
    assert dry["ui_skipped"] == [{
        "section": "Goals",
        "why": "this section is not filed as build work, so no UI item was hung off it",
    }]


def test_a_split_section_carries_the_design_block_and_files_a_ui_item(client, auth):
    """An oversized section filed as pieces still carries ## Design, and one UI item depends on each piece."""
    from sqlalchemy import select

    from app.models import Link
    from tests.prd_approve import approve_id

    body = (
        "# Spec\n\n## Work\n"
        "Design: https://claude.ai/artifact/abc\n"
        "Pages: Fix Dialog, Tracker - File a Fix\n\n"
        "### One\ndo the first\n### Two\ndo the second\n### Three\ndo the third\n\n"
        "## Acceptance criteria\n"
        "### One\n- first is done\n### Two\n- second is done\n### Three\n- third is done\n"
    )
    prd = client.post("/api/prds", json={"title": "Split Design", "body": body}, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    work = next(p for p in dry["proposals"] if p["section"] == "Work")
    assert "## Design" in work["description"]
    assert "Fix Dialog" in work["description"] and "Tracker - File a Fix" in work["description"]
    assert [s["heading"] for s in work["sub_items"]] == ["One", "Two", "Three"]
    for sub in work["sub_items"]:
        assert "## Design" in sub["description"]
        assert "Fix Dialog" in sub["description"] and "Tracker - File a Fix" in sub["description"]
    assert len(dry["ui_items"]) == 1 and dry["ui_items"][0]["depends_on"] == "Work"

    approve_id(prd["id"])
    db = SessionLocal()
    out = prd_svc.decompose(
        db, prd_svc.get_prd(db, prd["id"]), create=True,
        splits={"Work": ["One", "Two", "Three"]},
        touchpoints={"Work": ["backend/app/work.py", "web/src/Work.tsx"]})
    items = [db.get(items_svc.Item, i) for i in out["created"]]
    pieces = [i for i in items if i.title.startswith("Implement: Work —")]
    ui = next(i for i in items if i.title == "Implement: Work UI")
    assert len(pieces) == 3
    for piece in pieces:
        assert "## Design" in piece.description
        assert "Fix Dialog" in piece.description
        assert piece.touchpoints == ["backend/app/work.py"]
    assert ui.touchpoints == ["web/src/Work.tsx"] and ui.prd_section == "Work"
    links = list(db.scalars(select(Link).where(Link.a == ui.id, Link.type == "dependency")))
    assert {lnk.b for lnk in links} == {piece.id for piece in pieces}
    db.close()


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


# ---- GRPH-1690: decompose proposes the dependencies between sections -------------------
# A section body that names another implementable section is a statement that it builds on
# it, and the direction of that statement is the whole feature: the section doing the naming
# is the DEPENDENT. Every test here asserts the direction on both the proposal and the stored
# row, because a reversed link is indistinguishable from a correct one in a list of links.

_LINE_PRD = (
    "# Maintenance\n\n"
    "## The line\n"
    "A rolling queue of fixes.\n\n"
    "## Draining the line\n"
    "Walk the line and merge each fix; the line has to exist first.\n\n"
    "## Acceptance criteria\n"
    "### The line\n- a line can be opened\n"
    "### Draining the line\n- the drain merges each fix\n"
)


def _dep_links(db, created):
    """Stored dependency rows among `created`, as (a, b) — a DEPENDS ON b."""
    from sqlalchemy import select
    from app.models import Link
    return [(r.a, r.b) for r in
            db.scalars(select(Link).where(Link.type == "dependency", Link.a.in_(created)))]


def test_decompose_proposes_depends_on_in_the_direction_the_body_says(client, auth):
    """Section B names section A's heading, so B depends on A."""
    prd = client.post("/api/prds", json={"title": "Line", "body": _LINE_PRD},
                      headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert [d["section"] for d in by["Draining the line"]["depends_on"]] == ["The line"]
    assert by["The line"]["depends_on"] == []
    only = by["Draining the line"]["depends_on"][0]
    assert only["why"] == 'body names the section heading "The line"'
    assert only["matched"] == "the line"
    assert dry["link_cycles"] == [] and dry["links_skipped"] == []
    assert dry["links"] == []


def test_decompose_create_writes_the_link_in_the_same_direction(client, auth):
    from sqlalchemy import select
    from app.models import Link
    from tests.prd_approve import approve_id

    prd = client.post("/api/prds", json={"title": "Line 2", "body": _LINE_PRD},
                      headers=auth).json()
    approve_id(prd["id"])
    made = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth).json()
    assert [(row["section"], row["depends_on"]) for row in made["links"]] == [
        ("Draining the line", "The line")]
    db = SessionLocal()
    by_section = {db.get(items_svc.Item, i).prd_section: i for i in made["created"]}
    rows = list(db.scalars(select(Link).where(Link.type == "dependency",
                                              Link.a.in_(made["created"]))))
    db.close()
    assert [(r.a, r.b) for r in rows] == [
        (by_section["Draining the line"], by_section["The line"])]
    assert 'body names the section heading "The line"' in rows[0].reason
    assert rows[0].confidence == prd_svc._INFERRED_LINK_CONFIDENCE


def test_decompose_links_a_term_one_section_defines_and_another_uses(client, auth):
    body = (
        "# Fleet\n\n"
        "## Supervisor\n"
        "Adds `gbfleet until`, the drain loop.\n\n"
        "## Wave runner\n"
        "Calls `gbfleet until` once per PRD.\n\n"
        "## Acceptance criteria\n"
        "### Supervisor\n- the drain runs\n### Wave runner\n- a wave drains\n"
    )
    prd = client.post("/api/prds", json={"title": "Fleet", "body": body}, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert [d["section"] for d in by["Wave runner"]["depends_on"]] == ["Supervisor"]
    assert by["Supervisor"]["depends_on"] == []
    assert by["Wave runner"]["depends_on"][0]["matched"] == "gbfleet until"


def test_decompose_reports_a_cycle_instead_of_creating_it(client, auth):
    from tests.prd_approve import approve_id

    body = ("# Cycle\n\n## Alpha\nReads the Beta output.\n\n## Beta\nReads the Alpha output.\n\n"
            "## Acceptance criteria\n### Alpha\n- alpha works\n### Beta\n- beta works\n")
    prd = client.post("/api/prds", json={"title": "Cycle", "body": body}, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert dry["link_cycles"][0]["cycle"] == ["Alpha", "Beta", "Alpha"]
    cut = dry["link_cycles"][0]["cut"]
    assert (cut["section"], cut["depends_on"]) == ("Alpha", "Beta")
    assert "cycle" in dry["link_cycles"][0]["reason"]
    assert [d["section"] for d in by["Beta"]["depends_on"]] == ["Alpha"]
    assert by["Alpha"]["depends_on"] == []

    approve_id(prd["id"])
    made = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth).json()
    db = SessionLocal()
    by_section = {db.get(items_svc.Item, i).prd_section: i for i in made["created"]}
    rows = _dep_links(db, made["created"])
    db.close()
    assert rows == [(by_section["Beta"], by_section["Alpha"])]
    assert len(made["link_cycles"]) == 1


def test_decompose_names_a_reference_it_cannot_link(client, auth):
    """A reference to a section that already has work is reported, not silently dropped."""
    body = ("# Part\n\n## Ingest\nread the feed\n\n## Export\nwrite out what Ingest produced\n\n"
            "## Acceptance criteria\n### Ingest\n- feed is read\n### Export\n- rows written\n")
    prd = client.post("/api/prds", json={"title": "Part", "body": body}, headers=auth).json()
    client.post("/api/items", json={"title": "ingest, already built", "prd_id": prd["id"],
                                    "prd_section": "Ingest"}, headers=auth)
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    assert [p["section"] for p in dry["proposals"]] == ["Export"]
    assert dry["proposals"][0]["depends_on"] == []
    assert [(s["section"], s["depends_on"]) for s in dry["links_skipped"]] == [
        ("Export", "Ingest")]
    assert "already has tracked work" in dry["links_skipped"][0]["reason"]


def test_mcp_caller_can_drop_and_add_links_per_section(client, auth):
    """The inference is a reading of prose, so the caller has to be able to correct it."""
    key = client.post("/api/api-keys", json={"name": "link-agent", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    body = (
        "# Chain\n\n"
        "## Store\nwrites rows\n\n"
        "## Index\nreads what Store wrote\n\n"
        "## Serve\nanswers queries\n\n"
        "## Acceptance criteria\n"
        "### Store\n- rows land\n### Index\n- rows indexed\n### Serve\n- queries answered\n"
    )
    prd = client.post("/api/prds", json={"title": "Chain", "body": body, "project_id": "core"},
                      headers=auth).json()
    plain = _mcp(client, key, "decompose_prd", {"prd_id": prd["id"]})["structuredContent"]
    by = {p["section"]: p for p in plain["proposals"]}
    assert [d["section"] for d in by["Index"]["depends_on"]] == ["Store"]
    assert by["Serve"]["depends_on"] == []

    fixed = _mcp(client, key, "decompose_prd", {
        "prd_id": prd["id"],
        "drop_links": [{"section": "Index", "depends_on": "Store"}],
        "add_links": [{"section": "Serve", "depends_on": "Index"}],
    })["structuredContent"]
    by = {p["section"]: p for p in fixed["proposals"]}
    assert by["Index"]["depends_on"] == []
    assert [d["section"] for d in by["Serve"]["depends_on"]] == ["Index"]
    assert by["Serve"]["depends_on"][0]["why"] == "added by the caller"

    missed = _mcp(client, key, "decompose_prd", {
        "prd_id": prd["id"],
        "drop_links": [{"section": "Serve", "depends_on": "Store"}],
        "add_links": [{"section": "Nope", "depends_on": "Store"}],
    })["structuredContent"]
    reasons = [s["reason"] for s in missed["links_skipped"]]
    assert any(r.startswith("no proposed link matched") for r in reasons)
    assert any(r == "names a section this PRD does not have" for r in reasons)
    assert [d["section"] for d in
            {p["section"]: p for p in missed["proposals"]}["Index"]["depends_on"]] == ["Store"]


def test_mcp_decompose_prd_advertises_the_link_surface():
    from app.mcp_server import TOOLS
    tool = next(t for t in TOOLS if t["name"] == "decompose_prd")
    assert "depends_on" in tool["description"]
    assert {"drop_links", "add_links", "splits"} <= set(tool["inputSchema"]["properties"])


def test_mcp_decompose_create_writes_the_links_it_proposed(client, auth):
    from tests.prd_approve import approve_id

    key = client.post("/api/api-keys", json={"name": "link-writer", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    prd = client.post("/api/prds", json={"title": "MCP Line", "body": _LINE_PRD,
                                         "project_id": "core"}, headers=auth).json()
    approve_id(prd["id"])
    made = _mcp(client, key, "decompose_prd",
                {"prd_id": prd["id"], "create": True})["structuredContent"]
    assert [(row["section"], row["depends_on"]) for row in made["links"]] == [
        ("Draining the line", "The line")]
    db = SessionLocal()
    by_section = {db.get(items_svc.Item, i).prd_section: i for i in made["created"]}
    rows = _dep_links(db, made["created"])
    db.close()
    assert rows == [(by_section["Draining the line"], by_section["The line"])]


def test_decompose_links_a_slice_label_when_the_heading_is_not_quoted(client, auth):
    body = (
        "# Spec\n\n"
        "## S1 — The line\n"
        "A rolling queue.\n\n"
        "## S2 — Draining\n"
        "Runs after S1.\n\n"
        "## Acceptance criteria\n"
        "### S1 — The line\n- a line can be opened\n"
        "### S2 — Draining\n- the drain merges each fix\n"
    )
    prd = client.post("/api/prds", json={"title": "Slices", "body": body}, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert [d["section"] for d in by["S2 — Draining"]["depends_on"]] == ["S1 — The line"]
    assert by["S1 — The line"]["depends_on"] == []
    only = by["S2 — Draining"]["depends_on"][0]
    assert only["matched"] == "S1"
    assert "slice label" in only["why"]
    assert dry["link_cycles"] == []
