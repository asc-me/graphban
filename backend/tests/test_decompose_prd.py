"""decompose_prd files a task a person can claim (GRPH-1895).

P92's shape: one `## Acceptance` list, D-labelled sections, paths named as reads,
an oversized section with no `###` heading, and a later section that mentions an
earlier label. The dry run used to return nothing fileable.
"""
from types import SimpleNamespace

from app.services import prds as prd_svc


_P92_SHAPE = """\
# Rules import

## D1. Sources and who reads them
Parsers read each source.

## D2. Three buckets
Every finding lands in one bucket.

## D3. The import run and its report
An import run is a record.

## D4. The skill
The skill mentions D5 and `gban rules import`.
It files findings into the import run.

## D5. Rescan and drift
Rescan mentions D4 and `gban rules import`.
It files differences into the import run.

## Acceptance
- Each source is read by its parser
- A finding is one of the three buckets
- The import run stores the commit and the report shows it
- The skill files only through the tool
- A rescan reports drift and changes nothing else
- The moon is cheese
"""

_D1 = "D1. Sources and who reads them"
_D2 = "D2. Three buckets"
_D3 = "D3. The import run and its report"
_D4 = "D4. The skill"
_D5 = "D5. Rescan and drift"


def test_a_flat_acceptance_list_maps_onto_the_section_it_names(client, auth):
    """One ## Acceptance list, no ### groups. A clause that names a subject lands there.

    No section is blocked for "no acceptance" once a clause names it. A clause that
    names nothing stays unassigned, which is not a clean pass over the list.
    """
    prd = client.post("/api/prds", json={"title": "P92 shape", "body": _P92_SHAPE},
                      headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert set(by) == {_D1, _D2, _D3, _D4, _D5}
    assert by[_D1]["acceptance"] == ["Each source is read by its parser"]
    assert by[_D2]["acceptance"] == ["A finding is one of the three buckets"]
    assert by[_D3]["acceptance"] == [
        "The import run stores the commit and the report shows it"]
    assert by[_D4]["acceptance"] == ["The skill files only through the tool"]
    assert by[_D5]["acceptance"] == ["A rescan reports drift and changes nothing else"]
    assert dry["unassigned_clauses"] == ["The moon is cheese"]
    assert dry["no_acceptance"] == []
    for section in by.values():
        assert "acceptance" not in (section["blocker"] or "")


def test_read_paths_are_not_write_touchpoints_and_low_scores_need_a_name(client, auth, monkeypatch):
    """`reads AGENTS.md` is not a write. A search hit under the floor needs the path named.

    The floor is above the 0.61–0.66 band that proposed unrelated files on P92.
    """
    assert prd_svc._TP_SCORE_FLOOR > 0.66

    def fake_search(_db, _query, _project_id, top_k=5):
        return [
            (SimpleNamespace(path="fleet/tests/test_ci_red_log_read.py"), 0.66),
            (SimpleNamespace(path="backend/app/parser.py"), 0.66),
            (SimpleNamespace(path="backend/app/services/import_run.py"), 0.80),
            (SimpleNamespace(path="AGENTS.md"), 0.91),
        ][:top_k]

    monkeypatch.setattr("app.services.code_graph.search_code", fake_search)
    from app.db import SessionLocal
    from app.services import code_graph

    prd = client.post("/api/prds", json={
        "title": "Reads",
        "body": (
            "# Reads\n\n## Sources\n"
            "The skill reads `AGENTS.md`. Change `backend/app/parser.py` to trim.\n\n"
            "## Acceptance\n- Each source is read by its parser\n"
        ),
    }, headers=auth).json()
    db = SessionLocal()
    pid = db.get(prd_svc.Prd, prd["id"]).project_id
    code_graph.describe_code(db, project_id=pid, nodes=[
        {"path": "AGENTS.md", "kind": "file", "summary": "agent guide"},
    ])
    db.close()

    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    touch = by["Sources"]["touchpoints"]
    assert "AGENTS.md" not in touch
    assert "fleet/tests/test_ci_red_log_read.py" not in touch
    assert "backend/app/parser.py" in touch
    assert "backend/app/services/import_run.py" in touch
    evidence = {e["path"]: e["evidence"] for e in by["Sources"]["touchpoint_evidence"]}
    assert "also names the path" in evidence["backend/app/parser.py"]
    assert evidence["backend/app/services/import_run.py"].startswith("code search matched")
    assert "acceptance" not in (by["Sources"]["blocker"] or "")


def test_oversized_section_splits_or_is_refused(client, auth):
    """Bold list pieces become sub_items. A wall of text is refused, never an empty split."""
    blob = "x" * 3001
    body = (
        "# Spec\n\n## Wide\n"
        "- **Parsers.** read the workflows\n"
        "- **The skill.** reads the prose\n\n"
        f"## Blob\n{blob}\n\n"
        "## Acceptance\n"
        "### Parsers\n- parsers emit a proposal\n"
        "### The skill\n- the skill files guidance\n"
        "### Parsers\n- parsers report an unread source\n"
        "### Blob\n- the blob is stored\n"
    )
    prd = client.post("/api/prds", json={"title": "Split or refuse", "body": body},
                      headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    wide = by["Wide"]
    assert wide["oversized"] is True
    assert wide["oversized_reason"] == "acceptance_clauses 3 > 2"
    assert wide["split_refused"] == ""
    assert [s["heading"] for s in wide["sub_items"]] == ["Parsers", "The skill"]
    assert wide["sub_items"][0]["clauses"] == [
        "parsers emit a proposal", "parsers report an unread source"]
    assert wide["sub_items"][1]["clauses"] == ["the skill files guidance"]

    refused = by["Blob"]
    assert refused["sub_items"] == []
    assert refused["oversized"] is False
    assert refused["split_refused"]
    assert "body_chars" in refused["split_refused"]
    assert "no sub-heading" in refused["split_refused"]
    assert any(s["signal"] == "body_chars" and s["exceeded"] for s in refused["size"])
    assert "oversized section refused" in refused["blocker"]

    for proposal in dry["proposals"]:
        assert not (proposal["oversized"] and proposal["sub_items"] == [])


def test_import_run_dependencies_follow_the_record_not_the_label(client, auth):
    """D4 and D5 file into the import run D3 defines. Mentioning D4, D5, or the command does not.

    The reverse — D3 depends on D4 because the body says "D4", D4 depends on D5 because
    both mention `gban rules import` — is the P92 failure.
    """
    prd = client.post("/api/prds", json={"title": "P92 deps", "body": _P92_SHAPE},
                      headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert [d["section"] for d in by[_D4]["depends_on"]] == [_D3]
    assert [d["section"] for d in by[_D5]["depends_on"]] == [_D3]
    assert by[_D3]["depends_on"] == []
    assert by[_D1]["depends_on"] == []
    assert by[_D2]["depends_on"] == []
    for section in (_D4, _D5):
        only = by[section]["depends_on"][0]
        assert only["matched"] == "import run"
        assert "record" in only["why"]
        assert "gban rules import" not in only["matched"]
    assert dry["link_cycles"] == []


def test_a_route_one_section_defines_is_what_the_caller_depends_on(client, auth):
    body = (
        "# API\n\n"
        "## Writer\n"
        "Defines the route `POST /api/imports`.\n\n"
        "## Caller\n"
        "Posts each finding to `POST /api/imports`.\n\n"
        "## Acceptance\n### Writer\n- the route exists\n### Caller\n- a post lands\n"
    )
    prd = client.post("/api/prds", json={"title": "Route", "body": body}, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert [d["section"] for d in by["Caller"]["depends_on"]] == ["Writer"]
    assert by["Writer"]["depends_on"] == []
    only = by["Caller"]["depends_on"][0]
    assert only["matched"] == "POST /api/imports"
    assert "route" in only["why"]
