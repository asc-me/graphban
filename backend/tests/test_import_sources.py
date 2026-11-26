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
    f, u = S.parse_codeowners(OWNERS, "CODEOWNERS", commit="abc")
    prop = next(x for x in f if x["bucket"] == "proposed")["proposal"]
    assert prop["params"] == {"paths": ["db/migrations"]} and prop["rule_id"] == "plan.exclusive_areas"
    assert any(x["bucket"] == "gap" for x in f)
    assert u == [{"path": "CODEOWNERS:4", "reason": "outside this project's repo"}]


def test_workflow_engine_evidence_and_bad_yaml_unread():
    f, u = S.parse_workflow(WF, ".github/workflows/ci.yml", commit="abc")
    assert f[0]["proposal"]["params"] == {"path": "backend/**", "engine": "postgres"}
    f, u = S.parse_workflow("a: [", ".github/workflows/x.yml", commit="abc")
    assert f == [] and u and u[0]["path"] == ".github/workflows/x.yml"


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
        id="build.generated_files", stage="build", title="Generated files", summary="s",
        enforcement="checked", enforcer=R.Enforcer("supervisor", "gbfleet until"),
        default_on=True, default_params={"paths": ["old/**"]}))
    monkeypatch.setattr(RP, "FILE_READER", lambda repo, path, commit: path != "gone.md")
    body = S.read_sources({".gitattributes": "web/dist/** linguist-generated\n"}, commit="abc123",
                        current={"build.generated_files": {"paths": ["old/**"]}})
    body["findings"].append({"bucket": "proposed", "stage": "Build", "quote": "q",
                             "source": {"path": "gone.md"},
                             "proposal": {"kind": "tighten", "rule_id": "build.generated_files",
                                          "params": {"paths": ["old/**", "z"]}, "why": "w",
                                          "citations": S._cite("gone.md", "abc123")}})
    body.pop("clean")
    rep = client.post(f"/api/projects/{pid}/rules/import-runs", json=body, headers=h)
    assert rep.status_code == 201, rep.text
    fs = rep.json()["findings"]
    assert fs[0]["proposal"]["loosens"] is True
    assert "gone.md" in fs[1]["refused"] and "abc123" in fs[1]["refused"]
