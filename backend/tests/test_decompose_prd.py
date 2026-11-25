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


# The real GRPH-P92 body, verbatim (`get_prd GRPH-P92` v1.1). The hash in
# test_the_p92_fixture_is_the_real_p92_body is what pins it: GRPH-1895 passed on a
# synthetic P92 *shape* while the real PRD still filed nothing claimable, so a fixture
# that merely looks like P92 is the defect being fixed, and a hash is the one check on
# that which a lookalike cannot satisfy (GRPH-1905).
_P92_BODY = """\
# Rules import: bring an org's existing engineering requirements into the rules engine

**Status:** draft, 2026-11-19. Second of two PRDs. Depends on GRPH-P91, the custom-rules pipeline (proposals, citations, tighten-only, person adoption, observe-before-enforce). This PRD adds proposal sources. It adds no adoption path of its own.

**One sentence:** when an org sets up a project, Graphban reads the requirements the team has already written down and files them as cited proposals. Deterministic parsers cover config files and an agent skill covers prose. A person adopts them in one reviewed batch, and a rescan proposes only what changed.

## Problem

<!-- framing -->

- A new org arrives with rules it already follows. They are written in CI workflows, `CODEOWNERS`, branch protection, generated-file markers, `CONTRIBUTING.md`, ADRs, architecture docs and sometimes an `AGENTS.md`.
- Set up rules (`web/src/features/rules/SetupWizard.tsx`, P82) asks the person questions. It reads nothing in the repo, so the team retypes requirements it already has, and the ones nobody remembers to type are silently missing.
- Those sources change after onboarding. Nothing notices when the Rules page and the repo disagree.
- Reading prose with a model can invent a requirement. The pipeline PRD's guarantees exist for exactly that kind of input: every proposal is cited, nothing loosens, and a person adopts. This import must not route around any of them.

## Goals

<!-- framing -->

1. **One command, `gban rules import`,** reads a project's repo and its forge settings and files proposals through the pipeline.
2. **Structured sources are read by tested parsers, not a model.**
3. **Prose sources are read by the customer's own agent through a skill.** The skill files through the same `propose_rule` tool, so the server enforces everything the skill asks for.
4. **Every finding lands in one of three buckets,** and the import report shows all three:
   - a proposed rule;
   - an EXTERNAL rule that is enforced outside Graphban;
   - guidance plus a primitive gap.
5. **Adoption is one reviewed batch per import,** shown as a diff per stage.
6. **A rescan proposes only differences** against what is adopted, and can run from CI.

## Non-goals

<!-- framing -->

- **A hook** in any one harness. Imports are org acts, and customers use many harnesses.
- **Turning anything on without a person,** or adopting straight into enforcing. Pipeline D4 and D5 apply unchanged.
- **Weakening an invariant** because a team's documents allow it. For example, "self-merge is fine" does not switch off independent review. Invariants (`docs/rule-candidates.md`, "The line") are not proposable at all.
- **Rules about product architecture or style** beyond what primitives express. These become guidance plus a gap (pipeline D8).
- **Forges other than GitHub in v1,** and CI systems other than GitHub Actions in v1. The parser boundary is designed for more.
- **Writing to the customer's repo.** The import reads only.

## D1. Sources and who reads them

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
- **Agent guidance files are read, never replaced.** Findings from them are filed as proposals or guidance beside them. The import does not edit, generate or supersede those files (it only reads; see Non-goals).

## D2. Three buckets

Every finding lands in exactly one bucket:

1. **Proposed rule.** It maps to a primitive with params. It is filed as a pipeline proposal citing file, line range and commit.
2. **EXTERNAL rule.** It is real and already enforced outside Graphban, for example branch protection requiring two reviews. It is drawn on the Rules page at level EXTERNAL ("held outside Graphban, not verified"), with its source. When a primitive could also enforce it, a proposal for that primitive is filed alongside.
3. **Guidance plus gap.** It is real, but no primitive can express it. It is filed as pipeline D8 guidance, with its citation.

- **EXTERNAL states configuration, not safety.** Only a parser reading forge or CI configuration (D1: branch protection, rulesets, workflows) files an EXTERNAL rule; the prose skill never does. Graphban does not check that the outside enforcement works, and the label says so: "held outside Graphban, not verified". A setting that is configured but ineffective is still drawn as what is configured, with that label; it is never shown as held by Graphban.

- **A proposed rule targets only what the server enforces today.** That is a tighter value on an existing library rule (for example `build.engine_evidence`, `merge.done_predicates`, `build.generated_files`, `plan.exclusive_areas`), or a custom rule on a primitive the server evaluates for custom rules (`exclusive_areas` as of P91). A requirement whose primitive the server does not evaluate (for example CODEOWNERS review ownership, `reviewer_property`) is guidance plus a gap naming that primitive, never a proposal the pipeline would refuse or a rule that holds nothing. When a primitive gains an evaluator, a rescan proposes what was guidance.

## D3. The import run and its report

- **An import run is a record:** project, commit, sources read and unread, and each finding with its bucket. Its proposals carry the run id as proposer.
- **The report on the Rules page** groups findings by stage, then by bucket, each with its quote and source link. It shows unread sources first.
- **Adopt-the-batch** uses pipeline D4 batch adoption: the person ticks findings, gives one reason, and each rule's history records the run.

## D4. The skill

- **The `graphban-rules-import` skill tells the customer's agent:**
  - which prose sources to read;
  - how to match a requirement to a primitive using the primitive contract;
  - to quote the line it relies on;
  - to file guidance rather than stretch a primitive.
- **It runs in any harness with MCP.** It files only through `propose_rule`, attaching its findings to an import run the CLI opened (`gban rules import --prose` opens the run and prints the skill invocation).
- **The prose is read by the customer's own agent, on whatever model it runs.** There is no fallback to the box's local model in v1. A run with no agent attached lists every prose source as unread, so the report never reads as clean.
- **The skill's instructions guarantee nothing.** Every guarantee comes from the pipeline's server refusals.

## D5. Rescan and drift

- **`gban rules import` on a project that already has adopted rules** compares each finding with what is adopted. It files only:
  - new requirements;
  - tightened ones;
  - **source gone** notices for adopted rules that no finding at the new commit still supports.
- **A finding matches an adopted rule on what the rule says** (its primitive and params), not on where it was cited. A finding that matches an adopted rule but cites a new location (a moved or renamed file) is reported as **source moved**: shown in the report, nothing filed. So a refactor that moves documents does not flood the Rules page.
- **Source-gone and source-moved notices are shown, never acted on.** Loosening stays person-only. A notice can still prompt a person to change a rule.
- **A CI template** (GitHub Actions) runs the rescan when cited sources change on the default branch. It files proposals, never adopts, and reports unread sources as a failed step rather than a pass.
- **The template ships with the product:** `gban rules import --ci-template` prints the workflow, so it is versioned with the parsers whose findings it files. The docs link to the command rather than carry a copy.

## Acceptance

- On a fixture repo with a workflow that runs Postgres tests on `db/**`, a `CODEOWNERS` file where one team owns `billing/`, a `.gitattributes` marking generated files, and branch protection requiring two reviews, `gban rules import` files:
  - a `build.engine_evidence` tighten for `db/` on Postgres;
  - an `exclusive_areas` custom-rule proposal for `billing/`;
  - a `build.generated_files` tighten;
  - an EXTERNAL rule for two reviews;
  - guidance plus a gap naming `reviewer_property` for CODEOWNERS review ownership.

  Each cites file, lines and commit. Nothing is filed that the pipeline would refuse as unevaluated.
- With a token that cannot read branch protection, the report lists branch protection as unread and the run does not report clean.
- A `CONTRIBUTING.md` line requiring a layering rule no primitive expresses becomes guidance plus a gap. No rule is filed.
- A `CONTRIBUTING.md` line allowing self-merge produces nothing that loosens review, and the report says why.
- Adopting the batch with one reason gives every rule a history entry naming the run. All start `observing`.
- A rescan after the workflow adds a second engine files one `tighten` proposal and nothing else. A rescan after the workflow is deleted files a source-gone notice and changes no rule.
- Sabotage: let a parser return empty on an unreadable source, and the unread test fails. Let the skill path bypass `propose_rule`, and the citation-refusal test fails.
- Carried from the GRPH-P91 walk (GRPH-1783), walked on the import's fixture project, which has a repo set:
  - An import-run proposal adding a path to `build.generated_files` is accepted, flagged **loosens**, and cannot be batch-adopted; it is adopted alone with its own reason.
  - A proposal citing a file absent at the cited commit is refused, naming the file and the commit.

## Open questions

None open. Settled in the grill (author, 2026-11-24):

- One import run is one project and its repo in v1; cross-repo paths are unread; an org-wide batch is later, premium work (D1).
- A finding matches an adopted rule on primitive and params, not citation; a moved source is reported, not filed (D5).
- A missing forge permission is a report line naming the permission, not a setup step (D1).
- `AGENTS.md`, `CLAUDE.md`, `.cursorrules` and `.github/copilot-instructions.md` are skill sources; findings sit beside them (D1).
- The CI template ships with the product as `gban rules import --ci-template` (D5).
- Prose is read by the customer's own agent; no local-model fallback in v1 (D4).
"""

_P92_D1_HEADING = "D1. Sources and who reads them"
# The D1 the touchpoint test below was written against, taken from the body above rather
# than transcribed beside it, so the two fixtures cannot disagree about what P92 says.
# This is the section GRPH-1895's bounce measured: its sources sit in a markdown table
# row with no verb beside them, and the statement that they are read is five bullets
# further down.
_P92_D1 = prd_svc.section_bodies(_P92_BODY)[_P92_D1_HEADING]

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
        # No backticked `until`: since GRPH-1906 that name resolves to until.py on its own
        # and fills the last slot, so the score guard would go untested.
        "The drain posts to `backend/app/routers/fleet.py`. The server writes an alert in "
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


def test_the_p92_fixture_is_the_real_p92_body():
    """`_P92_BODY` is GRPH-P92 v1.1 byte for byte, not a shape that resembles it.

    GRPH-1895 shipped against the synthetic `_P92_SHAPE` above and its own commit recorded
    the real PRD as unchanged at 8 of 15 clauses: the fixtures passed while P92 still
    filed nothing claimable. The hash is the one check a lookalike fixture cannot satisfy,
    so the two tests below stay pointed at the PRD the defect was found on.
    """
    assert prd_svc.body_hash(_P92_BODY) == "49ef50d796f3048e"
    # The D1 the touchpoint test above pins is a slice of the same body, so the two
    # fixtures cannot drift apart into two different ideas of what P92 says.
    assert _P92_D1 == prd_svc.section_bodies(_P92_BODY)[_P92_D1_HEADING]


def test_a_clauses_sub_list_is_part_of_that_clause():
    """P92's first clause ends "files:" and then lists the five proposals the run files.

    One clause, not six. Flattened, the parent reached its task truncated at "files:" and
    each of the five children came back in `unassigned_clauses`, so the requirement was
    filed without the list of what it requires and five lines of it were nobody's
    (GRPH-1905).
    """
    clauses = [c for c, _group in prd_svc.acceptance_clauses(_P92_BODY)]
    # Eight items at the top level of P92's Acceptance. The flat count was 15: the seven
    # indented lines under two of them were being read as clauses in their own right.
    assert len(clauses) == 8

    first = next(c for c in clauses if c.startswith("On a fixture repo"))
    lines = first.splitlines()
    assert lines[0].endswith("`gban rules import` files:")
    assert [line.strip() for line in lines[1:6]] == [
        "- a `build.engine_evidence` tighten for `db/` on Postgres;",
        "- an `exclusive_areas` custom-rule proposal for `billing/`;",
        "- a `build.generated_files` tighten;",
        "- an EXTERNAL rule for two reviews;",
        "- guidance plus a gap naming `reviewer_property` for CODEOWNERS review ownership.",
    ]
    # Still indented, so the task's Acceptance renders the nested list the PRD had rather
    # than six siblings.
    assert all(line.startswith("  - ") for line in lines[1:6])
    # The indented paragraph after the sub-list is the same item's continuation, and it is
    # a requirement in its own right — dropping it would lose "each cites file, lines and
    # commit" from every task.
    assert lines[6].strip().startswith("Each cites file, lines and commit.")

    carried = next(c for c in clauses if c.startswith("Carried from the GRPH-P91 walk"))
    assert len(carried.splitlines()) == 3

    # No child survives as a clause of its own. This is the assertion the sabotage in the
    # item's third clause flattens: put the sub-lists back as separate clauses and it, and
    # the count above, go red.
    for child in (
        "a `build.engine_evidence` tighten for `db/` on Postgres;",
        "an `exclusive_areas` custom-rule proposal for `billing/`;",
        "a `build.generated_files` tighten;",
        "an EXTERNAL rule for two reviews;",
        "guidance plus a gap naming `reviewer_property` for CODEOWNERS review ownership.",
        "A proposal citing a file absent at the cited commit is refused, naming the file "
        "and the commit.",
    ):
        assert child not in clauses, child


def test_p92_real_body_leaves_no_clause_unassigned_and_no_section_unfed(client, auth):
    """The decompose GRPH-1905 was filed from: every clause placed, D1 and D2 both fed.

    Before, 7 of the 15 flattened lines mapped to no section, and D1 and D2 came back
    `no_acceptance` — so all five proposals were blocked though clauses exercise both.

    Neither section is reachable from its heading, which is why the fixture has to be the
    real body. D1 is "Sources and who reads them" and the clause that exercises it says
    "a token that cannot read branch protection … the run does not report clean": one
    subject word, which D3's heading also carries and outranks on hit count. D2 is "Three
    buckets" and no clause says "bucket". What places them is the section's own prose
    restating the clause — D1's bullet on a missing forge permission, D2's definition of
    the guidance-plus-gap bucket — and neither is in a synthetic shape.

    An empty `unassigned_clauses` here is not a switch that always answers empty: the same
    call on `_P92_SHAPE` still reports "The moon is cheese", in
    test_a_flat_acceptance_list_maps_onto_the_section_it_names.
    """
    prd = client.post("/api/prds", json={"title": "P92", "body": _P92_BODY},
                      headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert set(by) == {_D1, _D2, _D3, _D4, _D5}

    assert dry["unassigned_clauses"] == []
    assert dry["no_acceptance"] == []
    for section, proposal in by.items():
        assert proposal["acceptance"], section
        assert "acceptance" not in (proposal["blocker"] or ""), section

    # The two clauses the item named, on the two sections it named them for.
    assert any(c.startswith("With a token that cannot read branch protection")
               for c in by[_D1]["acceptance"]), by[_D1]["acceptance"]
    assert any(c.startswith("A `CONTRIBUTING.md` line requiring a layering rule")
               for c in by[_D2]["acceptance"]), by[_D2]["acceptance"]

    # The sub-list reaches the task that has to build it, still attached and still nested.
    holder = next(s for s in by
                  if any(c.startswith("On a fixture repo") for c in by[s]["acceptance"]))
    described = by[holder]["description"]
    assert "  - an EXTERNAL rule for two reviews;" in described
    assert "  Each cites file, lines and commit." in described
# ── GRPH-1906: on P92's real body every proposal was blocked for "no touchpoints" ────
#
# These run on `_P92_BODY` above: the whole real GRPH-P92 v1.1 body (body_hash
# 49ef50d796f3048e), not a shape beside it. The dry run that filed the item ran on that
# body, and a synthetic fixture is what let the previous pass report green while the real
# PRD still filed nothing claimable.

# The graph the dry run had. Paths are this repo's, and the summaries marked "live" were read
# back off the instance at revision 61bf37fa on 2026-11-24. Two groups are not live and say
# so: the `propose_rule` SYMBOL node, which the instance does not describe yet and which is
# the shape `describe_code` writes for one, and the CLI siblings a `gban rules` create is
# placed beside. The read-only paths D1 names are here so their exclusion is measured.
_P92_GRAPH = [
    {"path": "backend/app/services/rule_proposals.py", "kind": "file", "name": "",
     "summary": "Rule proposals: a cited, direction-checked suggestion a project can adopt "
                "(GRPH-1775 / P91 D2, D3)."},  # live
    {"path": "backend/app/services/rule_toggles.py", "kind": "file", "name": "rule_toggles.py",
     "summary": "Library rule toggles (GRPH-1262 / PRD-P63 §8, S2a): state and params, "
                "layered."},  # live
    {"path": "backend/app/services/rules_recommend.py", "kind": "file",
     "name": "rules_recommend.py",
     "summary": "Recommended rules (GRPH-1160 / PRD-P61 §5): rules a project's own evidence "
                "implies."},  # live
    {"path": "backend/tests/test_rule_engine_evidence.py", "kind": "file",
     "name": "test_rule_engine_evidence.py",
     "summary": "PRD-P63 §6 primitive 4/7 (GRPH-1271): `build.engine_evidence`, the "
                "`evidence_required` primitive bound to a library rule."},  # live
    {"path": "backend/alembic/versions/0167_rule_toggles.py", "kind": "file",
     "name": "0167_rule_toggles.py",
     "summary": "`rule_toggles`: per-scope state and params for library rules (GRPH-1262, "
                "PRD-P63 D4)."},  # live
    {"path": "web/src/features/rules/StageProposals.tsx", "kind": "file", "name": "",
     "summary": "One row per stage: how many proposals are open for it, with a link to the "
                "proposals strip"},  # live
    {"path": "web/src/features/rules/LibraryRuleControls.tsx", "kind": "file",
     "name": "LibraryRuleControls.tsx",
     "summary": "Library rule toggles on the Rules inspector (GRPH-1268 / PRD-P63 §8, "
                "S2c)."},  # live
    # Not live: the instance describes few symbols. This is the node `describe_code` writes
    # for one, and it is what D4's `propose_rule` resolves through when the graph has it.
    {"path": "backend/app/services/rule_proposals.py::propose_rule", "kind": "symbol",
     "name": "propose_rule", "summary": "File a cited proposal for a rule."},
    # Not live summaries; real paths. The package a `gban rules` module is created beside.
    {"path": "cli/src/gban/cli.py", "kind": "file", "name": "cli.py",
     "summary": "The `gban` command line: one act, one module."},
    {"path": "cli/src/gban/scan.py", "kind": "file", "name": "scan.py",
     "summary": "`gban scan`: run the code-health scanners and record the result."},
    # What D1 reads. Each must stay out of the proposal.
    {"path": "AGENTS.md", "kind": "doc", "name": "AGENTS.md", "summary": "Agent guide."},
    {"path": "CLAUDE.md", "kind": "doc", "name": "CLAUDE.md", "summary": "Agent guide."},
    {"path": "CONTRIBUTING.md", "kind": "doc", "name": "CONTRIBUTING.md",
     "summary": "Contributor guide."},
    {"path": ".cursorrules", "kind": "config", "name": ".cursorrules",
     "summary": "Editor agent rules."},
    {"path": ".gitattributes", "kind": "config", "name": ".gitattributes",
     "summary": "Generated-file markers."},
    {"path": ".github/workflows/ci.yml", "kind": "config", "name": "ci.yml",
     "summary": "The CI workflows."},
]

_P92_READ_ONLY = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".gitattributes",
                  ".cursorrules", ".github/copilot-instructions.md",
                  ".github/workflows/ci.yml")


def _quiet_semantic(monkeypatch):
    """Empty semantic hits, which is what the live box gives P92.

    Measured 2026-11-24 on the instance at revision 61bf37fa: a whole-section query peaks at
    0.71, and even an exact symbol-name query (`area_matches`) peaks at 0.72 — under
    `_TP_SCORE_FLOOR`, and none of those paths is named in the section, so the tier proposes
    nothing. An empty hit list is that result without an embedder in the test.
    """
    monkeypatch.setattr("app.services.code_graph.search_code", lambda *a, **k: [])


def _describe(pid, nodes):
    from app.db import SessionLocal
    from app.services import code_graph

    db = SessionLocal()
    try:
        code_graph.describe_code(db, project_id=pid, nodes=nodes)
    finally:
        db.close()


def _project_of(prd_id):
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return db.get(prd_svc.Prd, prd_id).project_id
    finally:
        db.close()


def _ns(path, kind="file", name="", summary=""):
    """A code-graph node, the shape `list_nodes` hands `propose_touchpoints`."""
    return SimpleNamespace(path=path, kind=kind, name=name, summary=summary)


def test_p92_real_body_files_a_touchpoint_for_every_section(client, auth, monkeypatch):
    """The dry run that filed GRPH-1906, on the body it was filed from.

    Every proposal came back `blocked: true` with `touchpoints: []`, which hands the split
    back to a person. Each section names something the graph can place — D1 `rules_recommend`
    and `build.engine_evidence`, D2 the same rule id, D3 the Rules page (it names no path and
    nothing backticked at all), D4 `propose_rule` and `gban rules import`, D5 that command
    again — and nothing it names only to read is proposed.

    Acceptance-clause assignment is GRPH-1905's item, so `blocked` itself is not asserted
    here: this test owns the "no touchpoints" half of it.
    """
    _quiet_semantic(monkeypatch)
    # The fixture really is the PRD: its D1 slice is the D1 the last pass pinned verbatim.
    assert prd_svc.section_bodies(_P92_BODY)[_P92_D1_HEADING].strip() == _P92_D1.strip()

    prd = client.post("/api/prds", json={"title": "P92 real", "body": _P92_BODY},
                      headers=auth).json()
    _describe(_project_of(prd["id"]), _P92_GRAPH)
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    by = {p["section"]: p for p in dry["proposals"]}
    assert set(by) == {_D1, _D2, _D3, _D4, _D5}
    assert dry["no_touchpoints"] == []
    for section, proposal in by.items():
        assert proposal["touchpoints"], section
        assert "no touchpoints" not in (proposal["blocker"] or ""), section
        assert not (set(proposal["touchpoints"]) & set(_P92_READ_ONLY)), section
        assert not ({e["path"] for e in proposal["touchpoint_evidence"]}
                    & set(_P92_READ_ONLY)), section

    # A backticked symbol resolves to the file that defines it, and the evidence names it.
    d4 = {e["path"]: e for e in by[_D4]["touchpoint_evidence"]}
    defined = d4["backend/app/services/rule_proposals.py"]
    assert "propose_rule" in defined["evidence"]
    assert "a symbol this file defines" in defined["evidence"]
    assert defined["create"] is False
    # D1's `rules_recommend` is the file-name tier, and D2's rule id the graph-entry tier:
    # each tier says which of them placed the path, so dropping one is visible.
    d1 = {e["path"]: e for e in by[_D1]["touchpoint_evidence"]}
    assert "which is this file" in d1["backend/app/services/rules_recommend.py"]["evidence"]

    # The module the command needs does not exist: a create beside its existing siblings,
    # marked as a create so a caller does not go looking for a file that is not there.
    created = d4["cli/src/gban/rules.py"]
    assert created["create"] is True
    assert "create" in created["evidence"] and "cli/src/gban" in created["evidence"]
    assert "cli/src/gban/rules.py" in by[_D5]["touchpoints"]

    # A rule id resolves through the graph too, with the id in its evidence.
    d2 = {e["path"]: e for e in by[_D2]["touchpoint_evidence"]}
    assert any("build.engine_evidence" in e["evidence"] and "code graph's entry" in e["evidence"]
               for e in d2.values()), d2

    # D3 names no path and nothing backticked: the page it draws the report on places it.
    d3 = {e["path"]: e for e in by[_D3]["touchpoint_evidence"]}
    assert "web/src/features/rules/" in d3
    assert "Rules page" in d3["web/src/features/rules/"]["evidence"]
    assert d3["web/src/features/rules/"]["create"] is False

    # D2 is over the clause signal, so it splits — and some of its pieces name no path, no
    # symbol and no page of their own. Filed with nothing they would be blocked for "no
    # touchpoints", which is this item's symptom one level down: a piece is part of the
    # section, and the section knows where the work lands. `touchpoints_inherited` says the
    # paths were borrowed rather than resolved, so a caller can narrow them per piece.
    # Which section splits is GRPH-1905's clause assignment, not this item's: it folded a
    # clause's sub-list into that clause and re-placed the sections' prose, so the split
    # this measured moved from D3 (now one clause) to D2.
    subs = {s["heading"]: s for s in by[_D2]["sub_items"]}
    assert len(subs) >= 2, by[_D2]["oversized_reason"]
    for heading, sub in subs.items():
        assert sub["touchpoints"], heading
        assert "no touchpoints" not in (sub["blocker"] or ""), heading
    borrowed = {h for h, s in subs.items() if s["touchpoints_inherited"]}
    resolved = {h for h, s in subs.items() if not s["touchpoints_inherited"]}
    # The marker is not a constant: one piece resolved its own path, another could not and
    # says it borrowed the section's. A piece that borrowed carries exactly the section's.
    assert borrowed and resolved, (sorted(borrowed), sorted(resolved))
    assert "EXTERNAL rule" in resolved, sorted(resolved)
    assert "Guidance plus gap" in borrowed, sorted(borrowed)
    for heading in borrowed:
        assert subs[heading]["touchpoints"] == by[_D2]["touchpoints"], heading


def test_a_backticked_name_resolves_to_the_file_that_defines_it():
    """Symbol node, then file name, then the graph's own description of a file.

    No path appears in the section, so the two tiers GRPH-1895 left see nothing and the name
    does all the work. Within a tier the definition site wins over a test or a migration of
    it, because the point is to name the file the work changes.
    """
    nodes = [
        _ns("backend/tests/test_rule_proposals.py", name="test_rule_proposals.py",
            summary="`propose_rule` refusals and folding."),
        _ns("backend/app/services/rule_proposals.py",
            summary="Rule proposals: a cited suggestion a project can adopt."),
        _ns("backend/app/services/rule_proposals.py::propose_rule", kind="symbol",
            name="propose_rule", summary="File a cited proposal."),
        _ns("backend/alembic/versions/0218_rule_proposals.py", name="0218_rule_proposals.py",
            summary="The `rule_proposals` table."),
    ]
    out = prd_svc._resolve_named_things(nodes, "D4. The skill\nIt files only through `propose_rule`.")
    assert [(path, create) for _o, path, _e, create in out] == [
        ("backend/app/services/rule_proposals.py", False)]
    assert "propose_rule" in out[0][2]
    # The symbol tier, not the graph-entry tier that would also reach this file by its
    # summary: dropping the symbol lookup has to be visible in what the evidence says.
    assert "a symbol this file defines" in out[0][2]

    # A rule id the graph describes in a module and in its test lands on the module — and
    # the test's path is the SHORTER of the two, so this is the ranking deciding, not length.
    ranked = prd_svc._resolve_named_things([
        _ns("backend/tests/test_engine_evidence.py", name="test_engine_evidence.py",
            summary="`build.engine_evidence` refusals."),
        _ns("backend/app/services/rule_engine_evidence.py", name="rule_engine_evidence.py",
            summary="Item moves, including the `build.engine_evidence` gate."),
    ], "A tighter `build.engine_evidence` for `db/`.")
    assert [path for _o, path, _e, _c in ranked] == [
        "backend/app/services/rule_engine_evidence.py"]
    assert "code graph's entry" in ranked[0][2]

    # A name only a file's own name carries takes the file-name tier.
    by_name = prd_svc._resolve_named_things(
        [_ns("backend/app/services/rule_toggles.py", name="rule_toggles.py",
             summary="State and params, layered.")],
        "A tighter value resolved by `rule_toggles`.")
    assert [path for _o, path, _e, _c in by_name] == [
        "backend/app/services/rule_toggles.py"]
    assert "which is this file" in by_name[0][2]

    # Control: a name the graph holds nowhere resolves to nothing. An empty list here is the
    # honest answer, and it is why the tier is not "propose something for every backtick".
    assert prd_svc._resolve_named_things(nodes, "Tighten `build.generated_files`.") == []


def test_a_command_with_no_module_is_a_create_beside_its_siblings():
    """`gban rules import` names a module that does not exist; its siblings say where it goes.

    The control is the point of the test: `gban scan` resolves to the module that already
    defines it and is NOT a create, so the marker is not a constant, and a program the graph
    holds no directory for places nothing at all rather than inventing one.
    """
    nodes = [
        _ns("cli/src/gban/cli.py", name="cli.py", summary="The `gban` acts."),
        _ns("cli/src/gban/scan.py", name="scan.py", summary="`gban scan`."),
        _ns("cli/src/gban/doctor.py", name="doctor.py", summary="`gban doctor`."),
    ]
    created = prd_svc._resolve_named_things(nodes, "Run `gban rules import --prose`.")
    assert [(path, create) for _o, path, _e, create in created] == [
        ("cli/src/gban/rules.py", True)]
    assert "create" in created[0][2] and "cli/src/gban" in created[0][2]

    existing = prd_svc._resolve_named_things(nodes, "Run `gban scan --all`.")
    assert [(path, create) for _o, path, _e, create in existing] == [
        ("cli/src/gban/scan.py", False)]

    assert prd_svc._resolve_named_things(nodes, "Run `docker compose up`.") == []


def test_a_named_page_resolves_to_where_the_graph_puts_it():
    """"The report on the Rules page" places D3, which names no path and no symbol.

    `.cursor/rules` holds more nodes than the Rules page's own directory, so this is the UI
    word deciding, not density: a page is drawn somewhere under `web/`.
    """
    nodes = [
        _ns("web/src/features/rules/StageProposals.tsx", summary="One row per stage."),
        _ns("web/src/features/rules/ProposalsStrip.tsx", summary="The proposals strip."),
        _ns(".cursor/rules/graphban.mdc", kind="config", name="graphban.mdc",
            summary="Editor rules."),
        _ns(".cursor/rules/ledger.mdc", kind="config", name="ledger.mdc",
            summary="Editor rules."),
        _ns(".cursor/rules/fleet.mdc", kind="config", name="fleet.mdc",
            summary="Editor rules."),
    ]
    out = prd_svc._resolve_named_things(
        nodes, "D3. The import run and its report\nThe report on the Rules page groups findings.")
    assert [(path, create) for _o, path, _e, create in out] == [
        ("web/src/features/rules/", False)]
    assert "Rules page" in out[0][2]

    # Control: a surface the graph has no directory for places nothing.
    assert prd_svc._resolve_named_things(nodes, "Drawn on the Billing page.") == []


def test_a_name_the_section_only_reads_is_not_a_touchpoint(client, auth, monkeypatch):
    """The name tier goes through the same read-only guard the path tier does.

    A section that resolves `AGENTS.md` by name and only reads it must not gain a write
    touchpoint just because the resolver is new.
    """
    _quiet_semantic(monkeypatch)
    body = (
        "# Spec\n\n## Sources\n"
        "The skill reads `AGENTS.md` and files what it finds.\n\n"
        "## Acceptance\n- each source is read\n"
    )
    prd = client.post("/api/prds", json={"title": "Reads by name", "body": body},
                      headers=auth).json()
    _describe(_project_of(prd["id"]), [{"path": "AGENTS.md", "kind": "doc",
                                        "name": "AGENTS.md", "summary": "Agent guide."}])
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    sources = {p["section"]: p for p in dry["proposals"]}["Sources"]
    assert sources["touchpoints"] == []
    assert sources["touchpoint_evidence"] == []


def _proposal_for(client, auth, nodes, body, title, section):
    """The decompose proposal for one section of a PRD described over `nodes`."""
    prd = client.post("/api/prds", json={"title": title, "body": body}, headers=auth).json()
    _describe(_project_of(prd["id"]), nodes)
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    return {p["section"]: p for p in dry["proposals"]}[section]


def test_a_resolved_page_yields_to_a_file_the_section_named_under_it(client, auth,
                                                                     monkeypatch):
    """"The Rules page" places a directory; a file named under it is the narrower answer.

    GRPH-1930. The section names `web/src/features/rules/RulesPage.tsx` verbatim, and the
    page it draws on resolves to the directory that file sits in. Both reached the proposal
    set, so a container held a slot its own file already held. The control is the same
    section with the file unnamed, where the directory still stands: what the first half
    asserts is the shadowing, not the surface tier going quiet.
    """
    _quiet_semantic(monkeypatch)
    nodes = [
        {"path": "web/src/features/rules/RulesPage.tsx", "kind": "file", "name": "",
         "summary": "The rules page."},
        {"path": "web/src/features/rules/StageProposals.tsx", "kind": "file", "name": "",
         "summary": "One row per stage."},
    ]

    named = _proposal_for(
        client, auth, nodes,
        "# Spec\n\n## Report\n"
        "The report on the Rules page groups findings, in "
        "`web/src/features/rules/RulesPage.tsx`.\n\n## Acceptance\n- it renders\n",
        "Page and its file", "Report")
    assert "web/src/features/rules/RulesPage.tsx" in named["touchpoints"]
    assert "web/src/features/rules/" not in named["touchpoints"]
    assert not [p for p in named["touchpoints"] if p.endswith("/")], named["touchpoints"]

    alone = _proposal_for(
        client, auth, nodes,
        "# Spec\n\n## Report\n"
        "The report on the Rules page groups findings.\n\n## Acceptance\n- it renders\n",
        "Page alone", "Report")
    assert alone["touchpoints"] == ["web/src/features/rules/"]


def test_a_backticked_container_name_yields_to_a_file_named_under_it(client, auth,
                                                                     monkeypatch):
    """A bare `app` reaches the directory `backend/app` through its stem.

    GRPH-1930's second case, and the one that needs no surface word: the section names
    `backend/app/routers/fleet.py`, so the container is both the same mention answered less
    narrowly and a prefix of a path already proposed. The control names the container alone,
    where nothing under it is proposed and `backend/app` still stands.
    """
    _quiet_semantic(monkeypatch)
    nodes = [
        {"path": "backend/app", "kind": "module", "name": "app",
         "summary": "The backend application package."},
        {"path": "backend/app/routers/fleet.py", "kind": "file", "name": "fleet.py",
         "summary": "Fleet routes."},
    ]

    named = _proposal_for(
        client, auth, nodes,
        "# Spec\n\n## Drain\n"
        "The drain reports through `app`, in `backend/app/routers/fleet.py`.\n\n"
        "## Acceptance\n- it reports\n",
        "Container and its file", "Drain")
    assert "backend/app/routers/fleet.py" in named["touchpoints"]
    assert "backend/app" not in named["touchpoints"]

    alone = _proposal_for(
        client, auth, nodes,
        "# Spec\n\n## Drain\n"
        "The drain reports through `app`.\n\n## Acceptance\n- it reports\n",
        "Container alone", "Drain")
    assert alone["touchpoints"] == ["backend/app"]
