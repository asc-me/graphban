"""P92 D1 (GRPH-1949): the structured-source parsers and the unread contract."""
from app.services import import_sources as S

WF = """
name: ci
on:
  pull_request:
    paths: ["backend/**"]
jobs:
  pg:
    runs-on: ubuntu-latest
    services:
      postgres: {image: "pgvector/pgvector:pg16"}
    steps:
      - run: pytest -q
"""
OWNERS = "# c\ndb/migrations @a\n*.md @a @b\nhttps://x.example/y @z\n"


def test_codeowners_area_gap_and_outside_repo():
    owners = OWNERS + "api/ @org/team-b\nweb/ @a\n"
    f, u = S.parse_codeowners(owners, "CODEOWNERS", commit="abc")
    props = [x["proposal"] for x in f if x["bucket"] == "proposed"]
    # One exclusive_areas CUSTOM rule per sole owner, never the library rule.
    assert [(p["kind"], p["primitive"], p["slug"], p["params"]) for p in props] == [
        ("new_rule", "exclusive_areas", "codeowners-a", {"paths": ["db/migrations", "web"]}),
        ("new_rule", "exclusive_areas", "codeowners-org-team-b", {"paths": ["api"]})]
    assert all(p["title"] and p["why"] for p in props)
    assert any(x["bucket"] == "gap" for x in f)
    assert u == [{"path": "CODEOWNERS:4", "reason": "outside this project's repo"}]


def test_workflow_proposes_turn_on_never_a_path_glob_and_bad_yaml_unread():
    f, u = S.parse_workflow(WF, ".github/workflows/ci.yml", commit="abc")
    props = [x["proposal"] for x in f if x["bucket"] == "proposed"]
    # One trigger directory (`backend/**`) is carried as the substring the gate matches.
    assert props == [{"kind": "turn_on", "rule_id": "build.engine_evidence",
                      "params": {"engine": "postgres", "path": "backend/"},
                      "why": props[0]["why"],
                      "citations": S._cite(".github/workflows/ci.yml", "abc")}]
    assert not [x for x in f if x["bucket"] == "gap"] and u == []
    # Trigger paths naming more than one directory, or no directory, stay a gap.
    for paths in ('["backend/**", "db/**"]', '["**/*.sql"]'):
        f, _ = S.parse_workflow(WF.replace('["backend/**"]', paths), "w.yml", commit="abc")
        props = [x["proposal"] for x in f if x["bucket"] == "proposed"]
        assert props[0]["params"] == {"engine": "postgres"}
        assert any(x["bucket"] == "gap" and x["label"] == "engine evidence paths" for x in f)
    f, u = S.parse_workflow("a: [", ".github/workflows/x.yml", commit="abc")
    assert f == [] and u and u[0]["path"] == ".github/workflows/x.yml"


def test_one_engine_proposal_across_jobs_and_files():
    two = WF + """  my:
    runs-on: ubuntu-latest
    services:
      mysql: {image: "mysql:8"}
    steps:
      - run: pytest
"""
    out = S.read_sources({".github/workflows/a.yml": two, ".github/workflows/b.yml": WF},
                         commit="abc")
    props = [x for x in out["findings"] if x["bucket"] == "proposed"]
    assert len(props) == 1 and props[0]["proposal"]["params"] == {"engine": "postgres",
                                                                  "path": "backend/"}
    assert any(x.get("label") == "engine evidence engine" and "mysql" in x["text"]
               for x in out["findings"])


def test_already_on_engine_rule_is_no_difference():
    out = S.read_sources({".github/workflows/ci.yml": WF}, commit="abc",
                         current={"build.engine_evidence": {"engine": "postgres", "_on": True}})
    assert not [x for x in out["findings"] if x["bucket"] == "proposed"]
    out = S.read_sources({".github/workflows/ci.yml": WF}, commit="abc",
                         current={"build.engine_evidence": {"engine": "mysql", "_on": True}})
    assert any(x.get("label") == "engine evidence engine" for x in out["findings"])
    # On with the same engine for another path: a gap naming both, never a changed path.
    out = S.read_sources({".github/workflows/ci.yml": WF}, commit="abc",
                         current={"build.engine_evidence": {"engine": "postgres", "_on": True,
                                                            "path": "alembic/versions/"}})
    gap = [x for x in out["findings"] if x.get("label") == "engine evidence paths"]
    assert not [x for x in out["findings"] if x["bucket"] == "proposed"]
    assert gap and "alembic/versions/" in gap[0]["text"] and "backend/" in gap[0]["text"]


FRESH_WF = """
on:
  pull_request:
    paths: ["db/**"]
jobs:
  postgres:
    runs-on: ubuntu-latest
    services:
      postgres: {image: "postgres:16"}
    steps:
      - run: pytest db/
"""


def test_fresh_project_proposes_against_no_platform_default(client, monkeypatch):
    """GRPH-1971. Clause 1: a project that has not set `build.engine_evidence` gets a filed
    proposal for `db/` on postgres, not a gap against the platform default's
    `alembic/versions/`. Clause 2: `.gitattributes` `gen/**` files a `build.generated_files`
    proposal carrying `gen/**` and nothing of the default list. Clause 3 (a project-set path
    still gaps) is `test_already_on_engine_rule_is_no_difference` and the tail of this test."""
    from app.db import SessionLocal
    from app.services import rule_pack as R
    from app.services import rule_proposals as RP
    from app.services import rule_toggles as T
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id="build.engine_evidence", stage="build", title="Engine evidence", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/items.py"),
        default_on=True, default_params={"path": "alembic/versions/", "engine": "postgres"}))
    monkeypatch.setattr(RP, "_PRIMITIVE_OF",
                        {**RP._PRIMITIVE_OF, "build.engine_evidence": "evidence_required"})
    T.register(T.LibraryRule(
        id="build.generated_files", stage="build", title="Generated files", summary="s",
        enforcement="checked", enforcer=R.Enforcer("supervisor", "gbfleet until"),
        default_on=True, default_params={"paths": ["old/**"]}))
    monkeypatch.setattr(RP, "FILE_READER", lambda repo, path, commit: True)
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Fresh"}, headers=h).json()["id"]
    with SessionLocal() as db:
        assert T.resolve(db, S.ENGINE_RULE, pid)["params"]["source"] == "platform default"
        cur = S.current_params(db, pid)
    assert cur["build.generated_files"] == {} and "path" not in cur[S.ENGINE_RULE]
    body = S.read_sources({".github/workflows/ci.yml": FRESH_WF,
                           ".gitattributes": "gen/** linguist-generated=true\n"},
                          commit="abc123", current=cur)
    assert not [f for f in body["findings"] if f["bucket"] == "gap"]
    props = {f["proposal"]["rule_id"]: f["proposal"] for f in body["findings"]
             if f["bucket"] == "proposed"}
    assert props[S.ENGINE_RULE]["params"] == {"engine": "postgres", "path": "db/"}
    assert props["build.generated_files"]["params"] == {"paths": ["gen/**"]}
    body.pop("clean")
    rep = client.post(f"/api/projects/{pid}/rules/import-runs", json=body, headers=h)
    assert rep.status_code == 201, rep.text
    filed = {f["proposal"]["rule_id"]: f for f in rep.json()["findings"] if f.get("proposal")}
    eng, gen = filed[S.ENGINE_RULE], filed["build.generated_files"]
    assert not eng["refused"] and eng["proposal"]["params"] == {"engine": "postgres",
                                                                 "path": "db/"}
    assert eng["proposal"]["direction"] == "tighten" and not eng["proposal"]["loosens"]
    assert not gen["refused"] and gen["proposal"]["params"] == {"paths": ["gen/**"]}
    assert gen["proposal"]["loosens"] is True  # adopted alone, by a person
    # Clause 3: once the project sets its own path, the same CI is a gap, never a new path.
    with SessionLocal() as db:
        from app.models import User
        T.set_project_toggle(db, pid, S.ENGINE_RULE,
                             {"params": {"path": "alembic/versions/", "engine": "postgres"}},
                             actor=db.query(User).first(), reason="this project's own path")
        cur = S.current_params(db, pid)
    out = S.read_sources({".github/workflows/ci.yml": FRESH_WF}, commit="abc123", current=cur)
    assert not [f for f in out["findings"] if f["bucket"] == "proposed"]
    assert [f for f in out["findings"] if f.get("label") == "engine evidence paths"]


REMOTE = """
on: [pull_request]
jobs:
  shared:
    uses: other-org/ci/.github/workflows/test.yml@v2
  local:
    uses: ./.github/workflows/local.yml
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: other-org/actions/lint@main
      - run: make
"""


def test_cross_repo_workflow_paths_are_unread_not_dropped():
    _, u = S.parse_workflow(REMOTE, ".github/workflows/ci.yml", commit="abc")
    assert [x["path"] for x in u] == [
        ".github/workflows/ci.yml: jobs.shared uses other-org/ci/.github/workflows/test.yml@v2",
        ".github/workflows/ci.yml: jobs.build.steps[1] uses other-org/actions/lint@main"]
    assert all(x["reason"] == "outside this project's repo" for x in u)
    out = S.read_sources({".github/workflows/ci.yml": REMOTE}, commit="abc")
    assert out["clean"] is False


def test_generated_markers():
    f = S.parse_gitattributes("web/dist/** linguist-generated\n", ".gitattributes", commit="abc")
    assert f[0]["proposal"]["params"] == {"paths": ["web/dist/**"]}
    files = {"uv.lock": "x", "a.py": "# DO NOT EDIT\n", "b.py": "ok"}
    assert S.generated_paths(files) == ["a.py", "uv.lock"]


def test_unreadable_branch_protection_is_unread_not_clean(monkeypatch):
    class R:
        status_code = 403
    monkeypatch.setattr(S.httpx, "get", lambda *a, **k: R())
    data, un = S.fetch_branch_protection("o/r", "main", "tok")
    assert data is None and "admin:read" in un["reason"]
    out = S.read_sources({"CODEOWNERS": OWNERS}, commit="abc", protection_unread=un)
    assert out["clean"] is False and un in out["sources_unread"]


def test_protection_findings():
    data = {"required_pull_request_reviews": {"required_approving_review_count": 2},
            "required_status_checks": {"contexts": ["ci"]}}
    out = S.read_sources({".github/workflows/ci.yml": WF}, commit="abc", protection=data)
    kinds = {(f["bucket"], f.get("label")) for f in out["findings"]}
    assert ("external", "required reviews") in kinds and out["clean"]
    assert any(f.get("proposal", {}).get("rule_id") == "merge.done_predicates"
               for f in out["findings"])


def test_parsed_findings_file_through_the_pipeline(client, monkeypatch):
    """Generated-file path proposal: filed, flagged loosens, not batch-adoptable; a file absent
    at the commit is refused naming file and commit."""
    from app.services import rule_proposals as RP
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=h).json()["id"]
    from app.services import rule_pack as R
    from app.services import rule_toggles as T
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id="build.engine_evidence", stage="build", title="Engine evidence", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/items.py"),
        default_on=False, default_params={"path": "alembic/versions/", "engine": "postgres"}))
    monkeypatch.setattr(RP, "_PRIMITIVE_OF",
                        {**RP._PRIMITIVE_OF, "build.engine_evidence": "evidence_required"})
    T.register(T.LibraryRule(
        id="build.generated_files", stage="build", title="Generated files", summary="s",
        enforcement="checked", enforcer=R.Enforcer("supervisor", "gbfleet until"),
        default_on=True, default_params={"paths": ["old/**"]}))
    monkeypatch.setattr(RP, "FILE_READER", lambda repo, path, commit: path != "gone.md")
    body = S.read_sources({".gitattributes": "web/dist/** linguist-generated\n",
                           ".github/workflows/ci.yml": WF, "CODEOWNERS": OWNERS},
                          commit="abc123",
                          current={"build.generated_files": {"paths": ["old/**"]}})
    body["findings"].sort(key=lambda f: f["source"]["path"] != ".gitattributes")
    body["findings"].append({"bucket": "proposed", "stage": "Build", "quote": "q",
                             "source": {"path": "gone.md"},
                             "proposal": {"kind": "tighten", "rule_id": "build.generated_files",
                                          "params": {"paths": ["old/**", "z"]}, "why": "w",
                                          "citations": S._cite("gone.md", "abc123")}})
    body.pop("clean")
    rep = client.post(f"/api/projects/{pid}/rules/import-runs", json=body, headers=h)
    assert rep.status_code == 201, rep.text
    fs = rep.json()["findings"]
    by_rule = {(f.get("proposal") or {}).get("rule_id"): f for f in fs if f.get("proposal")}
    gone = next(f for f in fs if f["source"]["path"] == "gone.md")
    assert "gone.md" in gone["refused"] and "abc123" in gone["refused"]
    gen = next(f for f in fs if f["source"]["path"] == ".gitattributes")
    assert gen["proposal"]["loosens"] is True and not gen["refused"]
    # The workflow's turn_on and the CODEOWNERS custom rule FILE; neither is refused.
    eng = by_rule["build.engine_evidence"]
    assert not eng["refused"] and eng["proposal"]["direction"] == "tighten"
    assert eng["proposal"]["params"]["path"] == "backend/"
    area = by_rule["custom.codeowners-a"]
    assert not area["refused"] and area["proposal"]["params"] == {"paths": ["db/migrations"]}
