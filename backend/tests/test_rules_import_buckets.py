"""GRPH-1950 (P92 D2): every finding lands in exactly one bucket."""
from app.services.rules_import import Citation, Finding, classify, EXTERNAL_LABEL

C = Citation("f", "1-2", "abc", "q")


def _f(**kw):
    base = dict(source_kind="ci", citation=C, stage="Build", title="t")
    base.update(kw)
    return Finding(**base)


def test_tighten_existing_library_rule_is_a_proposal():
    p = classify(_f(rule_id="build.generated_files", params={"paths": ["x/"]}))
    assert p.bucket == "proposal" and p.proposal["kind"] == "tighten"
    assert p.proposal["citations"][0]["ref"]["commit"] == "abc"


def test_exclusive_areas_custom_rule_is_a_proposal():
    p = classify(_f(source_kind="codeowners", slug="billing", primitive="exclusive_areas",
                    params={"paths": ["billing/"]}))
    assert p.bucket == "proposal" and p.proposal["kind"] == "new_rule"


def test_unevaluated_primitive_is_guidance_never_a_proposal():
    p = classify(_f(source_kind="codeowners", slug="rev", primitive="reviewer_property",
                    gap="reviewer_property"))
    assert p.bucket == "guidance" and p.proposal is None
    assert p.guidance["label"] == "reviewer_property"


def test_unexpressible_prose_is_guidance_and_files_no_rule():
    p = classify(_f(source_kind="prose", title="layering rule"))
    assert p.bucket == "guidance" and p.proposal is None


def test_external_from_parser_carries_label_and_optional_proposal():
    p = classify(_f(source_kind="forge", external=True, title="two reviews"))
    assert p.bucket == "external" and p.why == EXTERNAL_LABEL and p.proposal is None
    q = classify(_f(source_kind="forge", external=True, rule_id="merge.done_predicates",
                    params={"predicates": ["suite_green"]}))
    assert q.bucket == "external" and q.proposal["kind"] == "tighten"


def test_prose_never_files_external():
    p = classify(_f(source_kind="prose", external=True))
    assert p.bucket == "guidance" and p.external is None


def test_loosening_and_invariants_file_nothing():
    p = classify(_f(source_kind="prose", loosens=True, title="self-merge is fine"))
    assert p.bucket == "nothing" and "person-only" in p.why
    q = classify(_f(rule_id="review.not_your_own_work"))
    assert q.bucket == "nothing" and "invariant" in q.why


# ---- the payloads are fileable: feed them to the pipeline itself (bounce on attempt 1) -------

import pytest  # noqa: E402

from app.services import rule_proposals as RP  # noqa: E402


@pytest.fixture()
def project(client, monkeypatch):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com",
                                             "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Imp"}, headers=h).json()["id"]
    from app.services import rule_toggles as T
    from app.services import rules as R
    monkeypatch.setattr(T, "LIBRARY", dict(T.LIBRARY))
    T.register(T.LibraryRule(
        id=T.GENERATED_FILES_RULE, stage="build", title="Generated files", summary="",
        enforcement="checked",
        enforcer=R.Enforcer("graphban_source", "fleet/src/gbfleet/supervisor.py"),
        # The repo's own markers name a subset of the default, so the change is a tighten.
        default_on=True, default_params={"paths": ["gen/", "AGENTS.md"]}))
    seen = []
    monkeypatch.setattr(RP, "FILE_READER",
                        lambda repo, path, commit: seen.append((path, commit)) or True)
    return pid, seen


def _file(pid, payload):
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        row, _ = RP.propose(db, project_id=pid, proposer_kind="import", proposer="rules-import",
                            **payload)
        db.commit()
        assert row.direction == "tighten", row.direction
        return row.citations
    finally:
        db.close()


CI = Citation(".gitattributes", "3-4", "deadbeef", "gen/** linguist-generated")


def test_tighten_payload_is_accepted_by_propose(project):
    pid, seen = project
    p = classify(_f(citation=CI, rule_id="build.generated_files", params={"paths": ["gen/"]}))
    cites = _file(pid, p.proposal)
    assert seen == [(".gitattributes", "deadbeef")]
    assert cites[0]["ref"] == {"path": ".gitattributes", "commit": "deadbeef", "lines": "3-4"}


def test_new_rule_payload_is_accepted_by_propose(project):
    pid, _ = project
    owners = Citation(".github/CODEOWNERS", "7", "deadbeef", "billing/ @org/billing")
    p = classify(_f(source_kind="codeowners", citation=owners, slug="billing",
                    primitive="exclusive_areas", title="billing/ is owned by one team",
                    params={"paths": ["billing/"]}))
    assert p.proposal["why"] == "billing/ @org/billing"
    _file(pid, p.proposal)


def test_external_alongside_payload_is_accepted_by_propose(project):
    pid, _ = project
    p = classify(_f(source_kind="forge", citation=CI, external=True, title="generated files",
                    rule_id="build.generated_files", params={"paths": ["gen/"]}))
    assert p.bucket == "external" and p.external["source"]["ref"]["path"] == ".gitattributes"
    _file(pid, p.proposal)


# ---- build.engine_evidence for db/ on Postgres (bounce on attempt 2) -----------------------

WF = Citation(".github/workflows/ci.yml", "20-31", "deadbeef",
              "paths: ['db/**'] ... services: postgres")


@pytest.fixture()
def engine_rule(project, monkeypatch):
    """`build.engine_evidence` as the shipped pack binds it: a template on `evidence_required`."""
    from app.services import rule_pack as P
    from app.services import rule_toggles as T
    from app.services import rules as R
    rid = "build.engine_evidence"
    prim = P.PRIMITIVES["evidence_required"]
    T.register(T.LibraryRule(
        id=rid, stage="build", title="Engine evidence", summary="s", enforcement=prim.level,
        enforcer=R.Enforcer("graphban_source", "app/services/items.py"), default_on=False,
        default_params={"path": "alembic/versions/", "engine": "postgres"}))
    monkeypatch.setattr(P, "CURRENT", P.Pack(
        "t", "1.0.0", P.PRIMITIVE_API, "0.0.0",
        {rid: {"id": rid, "primitive": "evidence_required"}}, {}, {}, []))
    return project


def test_engine_evidence_for_db_on_postgres_is_a_fileable_tighten(engine_rule):
    """Acceptance: a workflow running Postgres tests on `db/**` files a `build.engine_evidence`
    tighten for `db/` on Postgres, cited by file, lines and commit, and the pipeline takes it."""
    pid, seen = engine_rule
    p = classify(_f(citation=WF, rule_id="build.engine_evidence",
                    params={"path": "db/", "engine": "postgres"}))
    # `path`/`engine` carry no comparator, so the tighten is filed as turning the rule on.
    assert p.bucket == "proposal" and p.proposal["kind"] == "turn_on"
    cites = _file(pid, p.proposal)
    assert seen == [(".github/workflows/ci.yml", "deadbeef")]
    assert cites[0]["ref"] == {"path": ".github/workflows/ci.yml", "commit": "deadbeef",
                               "lines": "20-31"}


def test_engine_evidence_already_on_is_refused_not_misfiled(engine_rule, client):
    """When the rule is already on, `propose` refuses the change (no direction between two
    paths); the classifier never dresses it up as a `tighten` the pipeline would compute."""
    pid, _ = engine_rule
    from app.services import rule_toggles as T
    T.LIBRARY["build.engine_evidence"] = T.LIBRARY["build.engine_evidence"].__class__(
        **{**T.LIBRARY["build.engine_evidence"].__dict__, "default_on": True})
    p = classify(_f(citation=WF, rule_id="build.engine_evidence",
                    params={"path": "db/", "engine": "postgres"}))
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        with pytest.raises(Exception, match="already on"):
            RP.propose(db, project_id=pid, proposer_kind="import", proposer="rules-import",
                       **p.proposal)
    finally:
        db.close()


# ---- End to end: `gban rules import` on a fixture repo (P92 D2 acceptance) -------------------
# The CLI's half (cli/src/gban/rules.py, stdlib only) is loaded from its file, so the body this
# test posts is exactly what `gban rules import` builds from a real git repo.

import importlib.util
import pathlib
import subprocess

import pytest

_CLI_RULES = pathlib.Path(__file__).resolve().parents[2] / "cli/src/gban/rules.py"
OWNER = "alex@ascme-labs.com"

FIXTURE = {
    ".github/workflows/db.yml": (
        "name: db\n"
        "on:\n"
        "  pull_request:\n"
        "    paths: [\"db/**\"]\n"
        "jobs:\n"
        "  pg:\n"
        "    runs-on: ubuntu-latest\n"
        "    services:\n"
        "      postgres: {image: \"postgres:16\"}\n"
        "    steps:\n"
        "      - run: pytest -q db/\n"),
    "CODEOWNERS": "# owners\nbilling/ @acme/billing-team\n",
    ".gitattributes": "gen/** linguist-generated\n",
    "CONTRIBUTING.md": ("# Contributing\n"
                        "Handlers never import from the storage layer directly.\n"
                        "Self-merge is fine for small fixes.\n"
                        "CI must be green before merge.\n"),
    "db/schema.sql": "create table t (id int);\n",
    "billing/charge.py": "x = 1\n",
}
TWO_REVIEWS = {"required_pull_request_reviews": {"required_approving_review_count": 2}}


def _cli_rules():
    spec = importlib.util.spec_from_file_location("gban_cli_rules", _CLI_RULES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_repo(root, files):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for path, text in files.items():
        f = root / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "fixture"], check=True)
    return root


def _mcp(client, key, args):
    return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "propose_rule", "arguments": args}},
                       headers={"X-API-Key": key}).json()["result"]


def _shipped_library(monkeypatch):
    """`build.engine_evidence` and `build.generated_files` as the shipped pack binds them."""
    from app.services import rule_pack as P
    from app.services import rule_toggles as T
    from app.services import rules as R
    monkeypatch.setattr(T, "LIBRARY", dict(T.LIBRARY))
    T.register(T.LibraryRule(
        id="build.engine_evidence", stage="build", title="Engine evidence", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/items.py"),
        default_on=False, default_params={"path": "alembic/versions/", "engine": "postgres"}))
    T.register(T.LibraryRule(
        id=T.GENERATED_FILES_RULE, stage="build", title="Generated files", summary="s",
        enforcement="checked", enforcer=R.Enforcer("supervisor", "gbfleet until"),
        default_on=True, default_params={"paths": ["uv.lock"]}))
    monkeypatch.setattr(P, "CURRENT", P.Pack(
        "t", "1.0.0", P.PRIMITIVE_API, "0.0.0",
        {"build.engine_evidence": {"id": "build.engine_evidence",
                                   "primitive": "evidence_required"},
         T.GENERATED_FILES_RULE: {"id": T.GENERATED_FILES_RULE,
                                  "primitive": "generated_files"}}, {}, {}, []))


@pytest.fixture()
def imported(client, monkeypatch, tmp_path):
    from app.services import import_sources as S
    from app.services import rule_proposals as RP
    _shipped_library(monkeypatch)
    seen = {}
    monkeypatch.setattr(RP, "FILE_READER", lambda repo, path, commit: path in FIXTURE)

    def protection(repo, branch, token=None):
        seen["repo"], seen["branch"] = repo, branch
        return TWO_REVIEWS, None
    monkeypatch.setattr(S, "fetch_branch_protection", protection)
    r = client.post("/api/auth/login", json={"email": OWNER, "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Fixture", "repo_url": "github.com/acme/shop"},
                      headers=h).json()["id"]
    proj = client.get(f"/api/projects/{pid}", headers=h).json()
    if proj.get("repo_url") != "github.com/acme/shop":
        assert client.patch(f"/api/projects/{pid}", json={"repo_url": "github.com/acme/shop"},
                            headers=h).status_code == 200
    key = client.post("/api/api-keys", json={"name": "a", "project_id": pid},
                      headers=h).json()["plaintext"]
    body = _cli_rules().import_body(_git_repo(tmp_path / "shop", FIXTURE))
    rep = client.post(f"/api/projects/{pid}/rules/import-runs", json=body, headers=h)
    assert rep.status_code == 201, rep.text
    return {"h": h, "pid": pid, "key": key, "body": body, "run": rep.json(), "seen": seen}


def _proposal(run, rule_id):
    return next(f for f in run["findings"]
                if (f.get("proposal") or {}).get("rule_id") == rule_id)


def _cites_file_lines_commit(f, commit):
    for c in (f.get("proposal") or {}).get("citations") or []:
        if c.get("kind") == "file":
            ref = c["ref"]
            assert ref["path"] and ref["lines"] and ref["commit"] == commit
            return True
    return False


def test_import_files_the_structured_acceptance(imported):
    """`gban rules import` on the fixture files each acceptance item, cited, none refused."""
    run, commit = imported["run"], imported["body"]["commit"]
    assert imported["seen"] == {"repo": "acme/shop", "branch": "main"}
    assert not [f for f in run["findings"] if f["refused"]], run["findings"]

    eng = _proposal(run, "build.engine_evidence")
    assert eng["proposal"]["kind"] == "turn_on" and eng["proposal"]["direction"] == "tighten"
    assert eng["proposal"]["params"]["engine"] == "postgres"
    assert eng["proposal"]["params"]["path"] == "db/"
    assert _cites_file_lines_commit(eng, commit)

    area = _proposal(run, "custom.codeowners-acme-billing-team")
    assert area["proposal"]["kind"] == "new_rule"
    assert area["proposal"]["primitive"] == "exclusive_areas"
    assert area["proposal"]["params"] == {"paths": ["billing"]}
    assert _cites_file_lines_commit(area, commit)

    gen = _proposal(run, "build.generated_files")
    # The adopted list plus the repo's markers. `generated_files` is `superset_loosens` (an
    # exempt path is checked less), so the pipeline flags this `tighten` as loosening and it
    # is adopted alone with its own reason (D1), never silently in the batch.
    assert gen["proposal"]["kind"] == "tighten"
    assert gen["proposal"]["params"]["paths"] == ["uv.lock", "gen/**"]
    assert gen["proposal"]["loosens"] is True
    assert _cites_file_lines_commit(gen, commit)

    ext = [f for f in run["findings"] if f["bucket"] == "external"]
    assert [(f["label"], f["proposal_id"]) for f in ext] == [("required reviews", None)]
    assert "2 approving" in ext[0]["text"]

    owners_gap = next(f for f in run["findings"] if f["bucket"] == "gap"
                      and f["source"]["path"] == "CODEOWNERS")
    assert "reviewer_property" in owners_gap["text"] and owners_gap["guidance_id"]
    # Nothing filed on a primitive the server does not evaluate for custom rules.
    from app.services import custom_rules
    for f in run["findings"]:
        p = f.get("proposal") or {}
        if p.get("kind") == "new_rule":
            assert p["primitive"] in custom_rules.EVALUATED
    # Prose waits for the customer's agent: listed unread, the run not clean.
    assert {s["path"] for s in run["sources_unread"]} == {"CONTRIBUTING.md"}
    assert run["clean"] is False


def test_contributing_lines_file_guidance_and_nothing_that_loosens(client, imported):
    """The skill's half on the same run: a layering rule no primitive expresses is guidance
    plus a gap and no rule; a self-merge line loosens nothing and the report says why; a prose
    line claiming enforcement elsewhere is never an EXTERNAL rule."""
    run, commit, key = imported["run"], imported["body"]["commit"], imported["key"]
    base = f"/api/projects/{imported['pid']}/rules/import-runs/{run['id']}"
    before = {f["proposal_id"] for f in run["findings"] if f["proposal_id"]}

    def cite(line):
        return [{"kind": "file", "ref": {"path": "CONTRIBUTING.md", "lines": line,
                                         "commit": commit}}]
    g = _mcp(client, key, {
        "import_run": run["id"], "kind": "guidance",
        "quote": "Handlers never import from the storage layer directly.",
        "draft": {"stage": "Build", "label": "layering: no primitive",
                  "text": "handlers must not import storage"}, "citations": cite("2")})
    assert not g.get("isError"), g
    # Self-merge: the worst an agent could file is switching off independent review. Refused.
    sm = _mcp(client, key, {
        "import_run": run["id"], "kind": "tighten", "quote": "Self-merge is fine for small fixes.",
        "draft": {"rule_id": "review.not_your_own_work", "params": {}}, "citations": cite("3")})
    assert sm.get("isError") is True
    ext = _mcp(client, key, {
        "import_run": run["id"], "kind": "external", "quote": "CI must be green before merge.",
        "draft": {"stage": "Merge", "label": "green CI", "text": "CI must pass"},
        "citations": cite("4"), "sources_read": ["CONTRIBUTING.md"]})
    assert not ext.get("isError"), ext

    rep = client.get(base, headers=imported["h"]).json()
    prose = {f["quote"]: f for f in rep["findings"] if f["source"]["path"] == "CONTRIBUTING.md"}
    lay = prose["Handlers never import from the storage layer directly."]
    assert lay["bucket"] == "gap" and lay["guidance_id"] and lay["proposal_id"] is None
    selfm = prose["Self-merge is fine for small fixes."]
    assert selfm["proposal_id"] is None and "invariant" in selfm["refused"]
    green = prose["CI must be green before merge."]
    assert green["bucket"] == "gap" and green["guidance_id"]
    assert "cannot record an EXTERNAL rule" in green["text"]
    # No proposal was filed by the prose at all; EXTERNAL stays the parser's one finding.
    after = {f["proposal_id"] for f in rep["findings"] if f["proposal_id"]}
    assert after == before
    assert [f["label"] for f in rep["findings"] if f["bucket"] == "external"] == [
        "required reviews"]
