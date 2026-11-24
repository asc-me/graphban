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
    # The section that DID split is not blocked for the refusal. `blocked` itself is
    # asserted in test_a_refused_split_is_blocked_not_claimable, where the section has
    # a clause and a touchpoint and the split refusal is the only thing left to block it.
    assert "oversized section refused" not in (wide["blocker"] or "")

    for proposal in dry["proposals"]:
        assert not (proposal["oversized"] and proposal["sub_items"] == [])


def test_a_refused_split_is_blocked_not_claimable(client, auth):
    """`split_refused` is what blocks the proposal (GRPH-1900).

    Blob carries a clause and a touchpoint, so `blocked` is True for one reason only:
    the section is oversized and has no piece to split on. That token is what stops
    create=True filing work nobody can do as claimable — dropping `or bool(split_refused)`
    from the proposal's blocked expression leaves every other test in this file green.
    """
    from app.db import SessionLocal
    from app.services import code_graph

    body = (
        "# Spec\n\n## Blob\n"
        "Create `backend/app/blob.py` to hold it.\n"
        + "x" * 3001
        + "\n\n## Acceptance\n### Blob\n- the blob is stored\n"
    )
    prd = client.post("/api/prds", json={"title": "Refused", "body": body},
                      headers=auth).json()
    db = SessionLocal()
    pid = db.get(prd_svc.Prd, prd["id"]).project_id
    code_graph.describe_code(db, project_id=pid, nodes=[
        {"path": "backend/app/blob.py", "kind": "file", "summary": "blob store"},
    ])
    db.close()

    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    blob = {p["section"]: p for p in dry["proposals"]}["Blob"]
    assert blob["split_refused"]
    assert blob["acceptance"] == ["the blob is stored"]
    assert "backend/app/blob.py" in blob["touchpoints"]
    assert "acceptance" not in (blob["blocker"] or "")
    assert "touchpoint" not in (blob["blocker"] or "")
    assert blob["blocked"] is True
    assert "oversized section refused" in blob["blocker"]


# The real GRPH-P92 D1, verbatim (`get_prd GRPH-P92` v1.1, body_hash 49ef50d796f3048e).
# This is the section the item was filed from and the one the bounce measured: its
# sources sit in a markdown table row with no verb beside them, and the statement that
# they are read is five bullets further down.
_P92_D1_HEADING = "D1. Sources and who reads them"
_P92_D1 = """\
| Source | Reader | Typical proposal |
|---|---|---|
| `.github/workflows/*.yml` | parser | a tighter `build.engine_evidence` (the engine a path's tests run on); required checks tighten `merge.done_predicates` |
| Branch protection and rulesets (GitHub API) | parser | EXTERNAL rule: required reviews, required checks, merge method; `gitops.merge_method` |
| `CODEOWNERS` | parser | an `exclusive_areas` custom rule where one owner holds an area; review ownership is guidance plus a gap (`reviewer_property`) |
| Generated-file markers (`linguist-generated` in `.gitattributes`, "DO NOT EDIT" headers, lockfiles) | parser | a tighter `build.generated_files` |
| `CONTRIBUTING.md`, ADRs, `docs/architecture*` | skill (the customer's agent) | anything mappable; otherwise guidance plus a gap |
| Agent guidance: `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.github/copilot-instructions.md` | skill (the customer's agent) | anything mappable; otherwise guidance plus a gap |

- **One import run is one project and its one repo** (v1). A path in `CODEOWNERS` or a workflow that points into another repo is listed as unread ("outside this project's repo"), never dropped. A combined org-wide batch across many repos is later work under org scope, which is premium (pipeline D7).
- **Parsers are product code** with fixtures and tests. A source a parser cannot read is reported as unread, never as empty: "could not read branch protection: token lacks admin:read" is not "no branch protection".
- **The forge token is the project's existing GitHub integration** (the one `rules_recommend` reads PRs with). The import asks for no new credential.
- **A missing forge permission is a report line, not a setup step.** The import runs with what the token can read. Branch protection the token cannot read is listed as unread, naming the permission it needs; the run is not reported clean.
- **Agent guidance files are read, never replaced.** Findings from them are filed as proposals or guidance beside them. The import does not edit, generate or supersede those files (it only reads; see Non-goals)."""

# What the bounce measured: D1's whole proposal was these four, all of them files the
# section reads.
_P92_GUIDANCE = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "CONTRIBUTING.md")


def test_p92_d1_sources_are_reads_not_write_touchpoints(client, auth):
    """The real P92 D1, not a synthetic adjacent-verb fixture (bounce defect 2).

    The first pass at this item passed its own test and still proposed
    ['.cursorrules', 'AGENTS.md', 'CLAUDE.md', 'CONTRIBUTING.md'] on the PRD it was
    filed from, because `_mention_role` stopped looking 80 characters back and the
    table row has no verb in it.
    """
    from app.db import SessionLocal
    from app.services import code_graph

    raw = f"{_P92_D1_HEADING}\n{_P92_D1}"
    for path in _P92_GUIDANCE:
        assert prd_svc._is_read_only_path(raw, path), path
    # Control: the same helper still calls a write a write, so the four results above
    # are not a function that answers "read" to everything.
    assert not prd_svc._is_read_only_path(
        "Create `backend/app/services/rules_import.py` for the parsers.",
        "backend/app/services/rules_import.py")
    # Control: a verb inside a quoted marker is text the section quotes, not work it
    # does. `"DO NOT EDIT" headers` sits nearer `CONTRIBUTING.md` than any read verb, so
    # counting it would paint the path a write again.
    assert prd_svc._is_read_only_path(
        'The skill reads the guidance. Markers say "DO NOT EDIT".\n`CONTRIBUTING.md`',
        "CONTRIBUTING.md")

    body = f"# Rules import\n\n## {_P92_D1_HEADING}\n{_P92_D1}\n\n## Acceptance\n- Each source is read by its parser\n"
    prd = client.post("/api/prds", json={"title": "P92 D1", "body": body},
                      headers=auth).json()
    db = SessionLocal()
    pid = db.get(prd_svc.Prd, prd["id"]).project_id
    code_graph.describe_code(db, project_id=pid, nodes=[
        {"path": p, "kind": "file", "summary": "agent guidance"} for p in _P92_GUIDANCE
    ])
    db.close()

    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    d1 = {p["section"]: p for p in dry["proposals"]}[_P92_D1_HEADING]
    assert not (set(d1["touchpoints"]) & set(_P92_GUIDANCE))
    assert not (set(e["path"] for e in d1["touchpoint_evidence"]) & set(_P92_GUIDANCE))
    # An absence that reads as a clean pass is the same defect wearing the other sign:
    # with every named path a read, D1 has no write touchpoint and says so.
    assert d1["touchpoints"] == []
    assert _P92_D1_HEADING in dry["no_touchpoints"]
    assert d1["blocked"] is True
    assert "touchpoint" in d1["blocker"]


def test_a_common_noun_defined_as_a_record_links_nobody(client, auth):
    """One ordinary word before "is a record" is a noun, not a name (bounce defect 1).

    "An item is a record" used to make every later section whose prose said "item"
    depend on Model, and docs/prd-20-living-graph.md's "Ours is a record of a codebase"
    defined a record named "Ours". A false dependency silently reorders somebody's drain.
    """
    body = (
        "# Ledger\n\n"
        "## Model\nAn item is a record with a status and an assignee.\n\n"
        "## Build\nThe builder claims an item and moves it on.\n\n"
        "## Graph\nOurs is a record of a codebase that agents are changing.\n\n"
        "## Finish\nThe work that is ours to finish.\n\n"
        "## Audit trail\nAn `Audit` is a record of every change.\n\n"
        "## Run book\nAn import run is a record: project, commit and findings.\n\n"
        "## Replay\nReplay reads the Audit row and files into the import run.\n\n"
        "## Acceptance criteria\n"
        "### Model\n- an item stores a status\n"
        "### Build\n- a claim moves the item\n"
        "### Graph\n- the graph follows the code\n"
        "### Finish\n- the work finishes\n"
        "### Audit trail\n- an audit row lands\n"
        "### Run book\n- a run stores the commit\n"
        "### Replay\n- a replay reads both\n"
    )
    prd = client.post("/api/prds", json={"title": "Nouns", "body": body},
                      headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert prd_svc._defined_names("An item is a record with a status.") == []
    assert prd_svc._defined_names("Ours is a record of a codebase.") == []
    assert by["Build"]["depends_on"] == []
    assert by["Finish"]["depends_on"] == []
    # Control: a backticked one-word name and a two-word name still link, so this is a
    # guard on ordinary nouns and not a switch that turns record inference off.
    assert [d["section"] for d in by["Replay"]["depends_on"]] == [
        "Audit trail", "Run book"]
    assert [d["matched"] for d in by["Replay"]["depends_on"]] == [
        "Audit", "import run"]


def test_decompose_prd_advertises_the_refusal():
    """The manifest names `split_refused`.

    A caller reading only the tool description has to know an oversized section can come
    back refused rather than `oversized: true` with an empty split — that empty split is
    the quiet absence this item was filed for.
    """
    from app.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "decompose_prd")
    assert "split_refused" in tool["description"]


def test_a_four_letter_plural_heading_still_receives_its_clause(client, auth):
    """`Runs` names a clause that says "run" (bounce, non-blocking).

    The stem floor was 4 letters, so Docs/Logs/Keys/Runs/Bugs matched nothing while
    Reports/Buckets/Sources worked, and a section headed by one of them came back
    blocked for "no acceptance" with its own clause sitting unassigned beside it.
    """
    assert prd_svc._best_subject_section("the run is stored", ["Runs"]) == "Runs"
    assert prd_svc._best_subject_section("a doc explains it", ["Docs"]) == "Docs"

    body = (
        "# Spec\n\n"
        "## Runs\nA run stores the commit.\n\n"
        "## Docs\nA doc explains it.\n\n"
        "## Acceptance\n- the run is stored\n- a doc explains the setting\n"
    )
    prd = client.post("/api/prds", json={"title": "Plurals", "body": body},
                      headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert by["Runs"]["acceptance"] == ["the run is stored"]
    assert by["Docs"]["acceptance"] == ["a doc explains the setting"]
    assert dry["unassigned_clauses"] == []
    assert dry["no_acceptance"] == []


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
