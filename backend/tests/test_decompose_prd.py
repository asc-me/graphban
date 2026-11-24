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


# The real GRPH-P99 §3, verbatim (`get_prd GRPH-P99` v1.0, body_hash 8947bf5b1c8e645f) —
# the section GRPH-1910 was filed from. Two `###` pieces, Server (backend + docs) and Bell
# (web), and a preamble that names no path: every path a piece can claim, its piece names.
_P99_S3_HEADING = "3. The server alert"
_P99_S3 = """\
Recipients: the PRD owner, falling back to the project's planners. The project is the PRD record's own; a PRD id that does not resolve in the caller's project is refused and alerts no one. There is at most one live drain alert per PRD (D6). The alert clears on `working` (D7): any open item ready or a live child.

### Server

`until` posts the notice as a fleet event (`POST /api/fleet/drain-notices` in `backend/app/routers/fleet.py`), naming the PRD, the outcome, and for a stall the open items with their reasons. The server turns it into an Alert with kind `drain_drained` or `drain_stalled`, on the same bell and Web Push path as `response_needed` (`backend/app/services/alerts.py`, `backend/app/services/push.py`). A drain alert has no wait item, so the `alerts` row gains a nullable `prd_id` (migration in `backend/alembic/versions/`), and `alert_dict` returns the PRD key for it. A new notice, including a reason change over the same open items, updates the live row in place. Web Push fires only when the outcome kind changes or 30 minutes after the last push for that PRD. A post identical to the server's last notice changes nothing. The route is listed in `docs/api-reference.md` and probed in `backend/tests/test_cross_tenant_writes.py`.

Acceptance:
- Posting a `drained` event writes one `drain_drained` alert for the PRD owner, returned by `GET /api/me/alerts` and sent by Web Push.
- Posting a `stalled` event writes a `drain_stalled` alert whose body lists each open item and its reason.
- Posting the same `stalled` event twice leaves one alert and sends one push.
- A `stalled` event with the same items but a changed reason updates the live alert body and sends no push inside 30 minutes.
- `stalled` → `drained` replaces the row and pushes.
- A later `working` event for the same PRD sets `cleared_at` on the live drain alert.
- A PRD with no owner alerts the project planners.
- An agent key from another project cannot post a drain event for this PRD.
- Backend suite on SQLite and Postgres passes. The migration adds the nullable column and leaves existing `response_needed` rows unchanged.

### Bell

`web/src/components/shell/AlertsBell.tsx` renders a drain alert with its outcome ("Drained" or "Stalled") and links it to the PRD page instead of a Triage item. `web/src/lib/types.ts` gains the two kinds.

Acceptance:
- A `drain_stalled` row in the bell shows "Stalled", the PRD key and the body, and its link opens the PRD page.
- A `drain_drained` row shows "Drained" and links to the PRD page.
- A `response_needed` row still links to Triage as before (`web/src/__tests__/alerts.test.tsx`).
- `pnpm typecheck` and the web tests pass."""

# The caller's list, as one decompose got it: thirteen paths across three trees, in an
# order that interleaves them so "in the order given" is a real assertion.
_P99_GIVEN = [
    "backend/app/routers/fleet.py",
    "web/src/components/shell/AlertsBell.tsx",
    "backend/app/services/alerts.py",
    "docs/api-reference.md",
    "web/src/lib/types.ts",
    "backend/app/services/push.py",
    "backend/app/models/__init__.py",
    "backend/alembic/versions/",
    "web/src/__tests__/alerts.test.tsx",
    "backend/tests/test_cross_tenant_writes.py",
    "docs/mcp.md",
    "web/src/lib/api.ts",
    "backend/tests/test_alerts.py",
]
# What each piece's own text names, in `_P99_GIVEN` order.
_P99_SERVER = [
    "backend/app/routers/fleet.py",
    "backend/app/services/alerts.py",
    "docs/api-reference.md",
    "backend/app/services/push.py",
    "backend/alembic/versions/",
    "backend/tests/test_cross_tenant_writes.py",
]
_P99_BELL = [
    "web/src/components/shell/AlertsBell.tsx",
    "web/src/lib/types.ts",
    "web/src/__tests__/alerts.test.tsx",
]
# Named by no piece: reported, and on neither child.
_P99_UNPLACED = [
    "backend/app/models/__init__.py",
    "docs/mcp.md",
    "web/src/lib/api.ts",
    "backend/tests/test_alerts.py",
]


def _p99_prd(client, auth) -> dict:
    body = f"# Drain outcome notices\n\n## {_P99_S3_HEADING}\n{_P99_S3}\n"
    return client.post("/api/prds", json={"title": "P99 §3", "body": body},
                       headers=auth).json()


def _p99_section(client, auth, touchpoints: dict) -> dict:
    """§3's proposal, decomposed as a service call because REST takes no `touchpoints`."""
    from app.db import SessionLocal

    prd = _p99_prd(client, auth)
    db = SessionLocal()
    try:
        out = prd_svc.decompose(db, prd_svc.get_prd(db, prd["id"]),
                                touchpoints=touchpoints)
    finally:
        db.close()
    return next(p for p in out["proposals"] if p["section"] == _P99_S3_HEADING)


def test_p99_pieces_get_only_the_given_paths_their_own_text_names(client, auth):
    """Server gets backend + docs, Bell gets web, and neither gets the other's.

    Both sub_items used to come back with all thirteen paths (`sub_tps = list(given)`),
    which is why the split existed and still did no work: the Bell child could edit the
    migration and the Server child the bell. A given path no piece names is reported on
    the proposal rather than copied onto every piece.
    """
    s3 = _p99_section(client, auth, {_P99_S3_HEADING: _P99_GIVEN})
    assert s3["oversized"] is True
    assert [s["heading"] for s in s3["sub_items"]] == ["Server", "Bell"]
    by = {s["heading"]: s for s in s3["sub_items"]}

    assert by["Server"]["touchpoints"] == _P99_SERVER
    assert by["Bell"]["touchpoints"] == _P99_BELL
    assert not (set(by["Server"]["touchpoints"]) & set(_P99_BELL))
    assert not (set(by["Bell"]["touchpoints"]) & set(_P99_SERVER))
    assert s3["unassigned_touchpoints"] == _P99_UNPLACED

    # Each piece is claimable on its own: its own paths, and the acceptance list its own
    # text carries (`Acceptance:` inline, which `parse_acceptance` reads at filing time).
    assert by["Server"]["blocker"] == "" and by["Bell"]["blocker"] == ""
    assert "Posting a `drained` event" in by["Server"]["description"]
    assert "renders a drain alert" in by["Bell"]["description"]
    # The section's own proposal still carries every path the caller gave, so a caller
    # that files the section whole loses nothing by the split being offered.
    assert s3["touchpoints"] == _P99_GIVEN


def test_filing_a_split_gives_each_item_only_its_own_paths(client, auth):
    """create=True: the distribution has to reach the filed rows, not only the proposal.

    The defect was read off a dry run, but the harm is on the item a child claims — the
    Bell child holding the migration path is what lets it edit another tree.
    """
    from app.db import SessionLocal
    from app.services import items as items_svc
    from tests.prd_approve import approve_id

    prd = _p99_prd(client, auth)
    approve_id(prd["id"])
    db = SessionLocal()
    out = prd_svc.decompose(
        db, prd_svc.get_prd(db, prd["id"]), create=True,
        splits={_P99_S3_HEADING: ["Server", "Bell"]},
        touchpoints={_P99_S3_HEADING: _P99_GIVEN})
    rows = {r.title: r for r in (db.get(items_svc.Item, i) for i in out["created"])}
    server = rows[f"Implement: {_P99_S3_HEADING} — Server"]
    bell = rows[f"Implement: {_P99_S3_HEADING} — Bell"]
    assert server.touchpoints == _P99_SERVER
    assert bell.touchpoints == _P99_BELL
    assert not (set(server.touchpoints) & set(_P99_BELL))
    assert not (set(bell.touchpoints) & set(_P99_SERVER))
    # Claimable, and split-linked to each other, so the drain takes them one at a time.
    assert {server.status, bell.status} == {"backlog"}
    assert {server.blocker, bell.blocker} == {""}
    db.close()


def test_a_piece_key_gives_one_piece_its_own_paths(client, auth):
    """`touchpoints={"<section> — <piece heading>": [...]}` wins for that piece.

    The pieces with no key of their own are still served from the section-level list, so
    the key is a correction and not a switch that turns distribution off. A given path
    the override displaces is reported: it is on no item, which the reply has to say.
    """
    # The key is matched on `_section_key`, so the dash, the spacing and the section
    # number are the caller's to write. A section-level key is not a piece key.
    assert prd_svc._piece_override(
        {"3. The server alert - Bell": ["web/src/Bell.tsx"]},
        _P99_S3_HEADING, "Bell") == ["web/src/Bell.tsx"]
    assert prd_svc._piece_override(
        {"the server alert—server": ["backend/app/x.py"]},
        _P99_S3_HEADING, "Server") == ["backend/app/x.py"]
    assert prd_svc._piece_override({_P99_S3_HEADING: _P99_GIVEN},
                                   _P99_S3_HEADING, "Bell") is None

    own = ["web/src/features/alerts/DrainAlert.tsx"]
    s3 = _p99_section(client, auth, {
        _P99_S3_HEADING: _P99_GIVEN,
        f"{_P99_S3_HEADING} — Bell": own,
    })
    by = {s["heading"]: s for s in s3["sub_items"]}
    assert by["Bell"]["touchpoints"] == own
    assert by["Server"]["touchpoints"] == _P99_SERVER
    assert s3["unassigned_touchpoints"] == [
        "web/src/components/shell/AlertsBell.tsx",
        "web/src/lib/types.ts",
        "backend/app/models/__init__.py",
        "web/src/__tests__/alerts.test.tsx",
        "docs/mcp.md",
        "web/src/lib/api.ts",
        "backend/tests/test_alerts.py",
    ]


def test_a_piece_no_given_path_names_says_where_they_went(client, auth):
    """A piece left with nothing is blocked for the reason that is true.

    "the code graph proposed none" is a different absence from "the caller gave paths
    and this piece named none of them", and only the second one is actionable from the
    reply: it names `unassigned_touchpoints` and the key that places them.
    """
    s3 = _p99_section(client, auth, {
        # Three trees, so the section is oversized and the split is offered; only the
        # Bell piece names none of them.
        _P99_S3_HEADING: ["backend/app/routers/fleet.py", "docs/api-reference.md",
                          "web/src/lib/api.ts"],
    })
    by = {s["heading"]: s for s in s3["sub_items"]}
    assert by["Bell"]["touchpoints"] == []
    assert by["Bell"]["blocker"]
    assert "none of the paths given for this section is named by this piece" in by["Bell"]["blocker"]
    assert "unassigned_touchpoints" in by["Bell"]["blocker"]
    # The piece that did name a path is not blocked, so the message is not blanket prose.
    assert by["Server"]["blocker"] == ""
    assert by["Server"]["touchpoints"] == [
        "backend/app/routers/fleet.py", "docs/api-reference.md"]


def test_unassigned_touchpoints_is_null_when_nothing_was_distributed(client, auth):
    """No split, no distribution: null, not an empty list that reads as all-placed.

    The section item carries every given path here. `[]` would say the pieces claimed
    them all, and there are no pieces.
    """
    body = (
        "# Spec\n\n## Small\none thing, in `backend/app/small.py`\n\n"
        "## Acceptance\n### Small\n- small is done\n"
    )
    prd = client.post("/api/prds", json={"title": "No split", "body": body},
                      headers=auth).json()
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        out = prd_svc.decompose(db, prd_svc.get_prd(db, prd["id"]), touchpoints={
            "Small": ["backend/app/small.py", "web/src/Small.tsx"]})
    finally:
        db.close()
    small = out["proposals"][0]
    assert small["sub_items"] == []
    assert small["unassigned_touchpoints"] is None
    assert small["touchpoints"] == ["backend/app/small.py", "web/src/Small.tsx"]


def test_a_directory_node_does_not_crowd_out_the_files_named_under_it(client, auth, monkeypatch):
    """P99 §3's graph shape: five slots, four of them containers (GRPH-1910).

    A directory node satisfies `path in text` as a prefix of every file named beneath it,
    so the proposal was [`backend/alembic`, `backend/alembic/versions`, `backend/app`,
    `backend/app/routers`, `backend/app/routers/fleet.py`] and `_TP_MAX` truncated the
    files the section actually named. A container with nothing named under it is still
    proposed — "a migration in `backend/alembic/versions/`" is the write target.
    """
    from app.db import SessionLocal
    from app.services import code_graph

    monkeypatch.setattr("app.services.code_graph.search_code", lambda *_a, **_k: [])

    # The predicate on its own, with the control that keeps it from being "drop every
    # directory": the second path is the same shape and must survive.
    assert prd_svc._shadowed_directory("backend/app", "in `backend/app/routers/fleet.py`", [])
    assert not prd_svc._shadowed_directory(
        "backend/alembic/versions", "a migration in `backend/alembic/versions/`", [])
    # A matched node under it is the other signal: `docs` matches on the bare word, and
    # the file under it is what the section named.
    assert prd_svc._shadowed_directory(
        "docs", "see docs and `api-reference.md`", ["docs/api-reference.md"])

    body = (
        "# Drain notices\n\n## Alert\n"
        "`until` posts to `backend/app/routers/fleet.py`, on the alert path in "
        "`backend/app/services/alerts.py`. A migration in `backend/alembic/versions/`. "
        "The route is listed in `docs/api-reference.md`.\n\n"
        "## Acceptance\n- the route posts an alert\n"
    )
    prd = client.post("/api/prds", json={"title": "Dirs", "body": body}, headers=auth).json()
    db = SessionLocal()
    pid = db.get(prd_svc.Prd, prd["id"]).project_id
    code_graph.describe_code(db, project_id=pid, nodes=[
        {"path": "backend/alembic", "kind": "module", "summary": "migrations"},
        {"path": "backend/alembic/versions", "kind": "module", "summary": "migration files"},
        {"path": "backend/app", "kind": "module", "summary": "the app"},
        {"path": "backend/app/routers", "kind": "module", "summary": "routers"},
        {"path": "backend/app/routers/fleet.py", "kind": "file", "summary": "fleet routes"},
        {"path": "backend/app/services/alerts.py", "kind": "file", "summary": "alerts"},
        {"path": "docs/api-reference.md", "kind": "doc", "summary": "the REST surface"},
    ])
    db.close()

    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    alert = {p["section"]: p for p in dry["proposals"]}["Alert"]
    assert set(alert["touchpoints"]) == {
        "backend/alembic/versions",
        "backend/app/routers/fleet.py",
        "backend/app/services/alerts.py",
        "docs/api-reference.md",
    }


def test_truncation_keeps_named_files_ahead_of_search_hits(client, auth, monkeypatch):
    """`_TP_MAX` cuts the weakest signal first, and a score cannot smuggle a shadow back.

    Four named files, one container the section named only as their prefix, and three
    search hits over the floor. Every named file keeps its slot; the container is out —
    including on a 0.99 score, which is how it would otherwise come straight back — and
    the one leftover slot goes to a search hit.
    """
    from app.db import SessionLocal
    from app.services import code_graph

    def fake_search(_db, _query, _project_id, top_k=5):
        return [
            (SimpleNamespace(path="backend/app"), 0.99),
            (SimpleNamespace(path="fleet/src/gbfleet/until.py"), 0.95),
            (SimpleNamespace(path="web/src/lib/api.ts"), 0.91),
        ][:top_k]

    monkeypatch.setattr("app.services.code_graph.search_code", fake_search)

    body = (
        "# Drain notices\n\n## Alert\n"
        "`until` posts to `backend/app/routers/fleet.py`. The server writes an alert in "
        "`backend/app/services/alerts.py` and pushes it from `backend/app/services/push.py`, "
        "probed in `backend/tests/test_cross_tenant_writes.py`.\n\n"
        "## Acceptance\n- the route posts an alert\n"
    )
    prd = client.post("/api/prds", json={"title": "Slots", "body": body}, headers=auth).json()
    db = SessionLocal()
    pid = db.get(prd_svc.Prd, prd["id"]).project_id
    code_graph.describe_code(db, project_id=pid, nodes=[
        {"path": "backend/app", "kind": "module", "summary": "the app"},
        {"path": "backend/app/routers/fleet.py", "kind": "file", "summary": "fleet routes"},
        {"path": "backend/app/services/alerts.py", "kind": "file", "summary": "alerts"},
        {"path": "backend/app/services/push.py", "kind": "file", "summary": "web push"},
        {"path": "backend/tests/test_cross_tenant_writes.py", "kind": "file", "summary": "probes"},
        {"path": "fleet/src/gbfleet/until.py", "kind": "file", "summary": "the drain loop"},
        {"path": "web/src/lib/api.ts", "kind": "file", "summary": "api client"},
    ])
    db.close()

    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    touch = {p["section"]: p for p in dry["proposals"]}["Alert"]["touchpoints"]
    assert "backend/app" not in touch
    assert touch[:4] == [
        "backend/app/routers/fleet.py",
        "backend/app/services/alerts.py",
        "backend/app/services/push.py",
        "backend/tests/test_cross_tenant_writes.py",
    ]
    assert touch[4:] == ["fleet/src/gbfleet/until.py"]

