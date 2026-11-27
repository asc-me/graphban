"""P92 D5 (GRPH-1953): a rescan files only differences, and reports drift without acting on it.

Walked through `POST /rules/import-runs/scan`, the route `gban rules import` calls, on a fixture
project whose adopted rules came from an earlier import.
"""
from app.db import SessionLocal
from app.models import RuleGuidance, RuleProposal
from app.services import rule_toggles as T

OWNER = "alex@ascme-labs.com"

#: CI runs the tests on the default engine only: no database service, so no engine finding.
WF_ONE = """
name: ci
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -q
"""
#: The same workflow after it adds a second engine: a Postgres service the tests run against.
WF_TWO = WF_ONE + """    services:
      postgres: {image: "postgres:16"}
"""
OWNERS = "billing/ @org/billing\n"
ATTRS = "web/dist/** linguist-generated\n"


def _setup(client, monkeypatch):
    from app.services import rule_pack as R
    from app.services import rule_proposals as RP
    monkeypatch.setattr(RP, "FILE_READER", lambda repo, path, commit: True)
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
        default_on=True, default_params={"paths": []}))
    r = client.post("/api/auth/login", json={"email": OWNER, "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=h).json()["id"]
    return h, pid


def _scan(client, h, pid, commit, files):
    r = client.post(f"/api/projects/{pid}/rules/import-runs/scan",
                    json={"commit": commit, "files": files}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _filed(rep):
    return [f for f in rep["findings"] if f["proposal_id"] or f["guidance_id"]]


def _adopt_all(client, h, pid, rep):
    # A generated-files path loosens (D1): it is adopted alone, the rest as one batch.
    props = [f for f in rep["findings"] if f["proposal_id"]]
    for ids in ([f["id"] for f in props if not f["proposal"]["loosens"]],
                *[[f["id"]] for f in props if f["proposal"]["loosens"]]):
        if not ids:
            continue
        r = client.post(f"/api/projects/{pid}/rules/import-runs/{rep['id']}/adopt",
                        json={"finding_ids": ids, "reason": "imported"}, headers=h)
        assert r.status_code == 200, r.text
    for f in rep["findings"]:
        if f["guidance_id"]:
            r = client.post(f"/api/projects/{pid}/rules/guidance/{f['guidance_id']}/adopt",
                            headers=h)
            assert r.status_code == 200, r.text


def _counts(pid):
    with SessionLocal() as db:
        return (db.query(RuleProposal).filter_by(project_id=pid).count(),
                db.query(RuleGuidance).filter_by(project_id=pid).count())


def _engine(pid):
    with SessionLocal() as db:
        st = T.resolve(db, "build.engine_evidence", pid)
        return st["on"]["value"], st["params"]["value"]


def test_second_engine_files_one_tighten_and_a_deleted_workflow_is_a_source_gone_notice(
        client, monkeypatch):
    h, pid = _setup(client, monkeypatch)
    files = {".github/workflows/ci.yml": WF_ONE, "CODEOWNERS": OWNERS, ".gitattributes": ATTRS}
    first = _scan(client, h, pid, "c1", files)
    assert {(f.get("proposal") or {}).get("rule_id") for f in first["findings"]
            if f["proposal_id"]} == {"custom.codeowners-org-billing", "build.generated_files"}
    _adopt_all(client, h, pid, first)
    before = _counts(pid)

    # The workflow adds a second engine. Everything else says what is adopted already.
    second = _scan(client, h, pid, "c2", {**files, ".github/workflows/ci.yml": WF_TWO})
    filed = _filed(second)
    assert len(filed) == 1, filed
    p = filed[0]["proposal"]
    assert (p["rule_id"], p["kind"], p["direction"]) == ("build.engine_evidence", "turn_on",
                                                         "tighten")
    assert p["proposer"] == f"import:{second['id']}"
    assert not [f for f in second["findings"] if f["bucket"] == "notice"]
    # Nothing re-filed and refused either: an adopted custom rule is matched, not re-proposed.
    assert not [f for f in second["findings"] if f["refused"]]
    assert _counts(pid) == (before[0] + 1, before[1])
    _adopt_all(client, h, pid, second)
    assert _engine(pid)[0] is True

    # The workflow is deleted: a source-gone notice, and no rule changes.
    on_before, params_before = _engine(pid)
    counts = _counts(pid)
    third = _scan(client, h, pid, "c3", {"CODEOWNERS": OWNERS, ".gitattributes": ATTRS})
    assert _filed(third) == []
    notices = [f for f in third["findings"] if f["bucket"] == "notice"]
    assert [(n["label"], n["source"]["path"]) for n in notices] == [
        ("source gone", ".github/workflows/ci.yml")]
    assert "build.engine_evidence" in notices[0]["text"]
    assert notices[0]["proposal_id"] is None and notices[0]["guidance_id"] is None
    assert _engine(pid) == (on_before, params_before) and _counts(pid) == counts
    # A notice is shown, never adopted.
    r = client.post(f"/api/projects/{pid}/rules/import-runs/{third['id']}/adopt",
                    json={"finding_ids": [notices[0]["id"]], "reason": "x"}, headers=h)
    assert r.status_code == 422


def test_a_moved_source_is_reported_not_filed(client, monkeypatch):
    h, pid = _setup(client, monkeypatch)
    first = _scan(client, h, pid, "c1", {"CODEOWNERS": OWNERS})
    _adopt_all(client, h, pid, first)
    counts = _counts(pid)
    moved = _scan(client, h, pid, "c2", {".github/CODEOWNERS": OWNERS})
    assert _filed(moved) == [] and _counts(pid) == counts
    labels = sorted((n["label"], n["source"]["path"]) for n in moved["findings"]
                    if n["bucket"] == "notice")
    # The custom rule and the review-ownership guidance both moved; neither is "gone".
    assert labels == [("source moved", ".github/CODEOWNERS"),
                      ("source moved", ".github/CODEOWNERS")]


def test_a_tightened_requirement_files_the_difference(client, monkeypatch):
    h, pid = _setup(client, monkeypatch)
    first = _scan(client, h, pid, "c1", {"CODEOWNERS": OWNERS, ".gitattributes": ATTRS})
    _adopt_all(client, h, pid, first)
    more = _scan(client, h, pid, "c2", {
        "CODEOWNERS": OWNERS + "invoices/ @org/billing\n",
        ".gitattributes": ATTRS + "api/gen/** linguist-generated\n"})
    by_rule = {f["proposal"]["rule_id"]: f["proposal"] for f in _filed(more) if f["proposal"]}
    area = by_rule["custom.codeowners-org-billing"]
    assert area["kind"] == "tighten" and area["direction"] == "tighten"
    assert area["params"] == {"paths": ["billing", "invoices"]}
    gen = by_rule["build.generated_files"]
    assert gen["params"] == {"paths": ["web/dist/**", "api/gen/**"]}
    # The review-ownership gap says what adopted guidance says: not filed again.
    assert not [f for f in _filed(more) if f["guidance_id"]]


def test_unread_source_is_not_called_gone(client, monkeypatch):
    h, pid = _setup(client, monkeypatch)
    first = _scan(client, h, pid, "c1", {"CODEOWNERS": OWNERS})
    _adopt_all(client, h, pid, first)
    r = client.post(f"/api/projects/{pid}/rules/import-runs/scan",
                    json={"commit": "c2", "files": {},
                          "sources_unread": [{"path": "CODEOWNERS", "reason": "unreadable"}]},
                    headers=h)
    rep = r.json()
    assert not [f for f in rep["findings"] if f["bucket"] == "notice"]
    assert rep["clean"] is False


def test_no_github_repo_lists_branch_protection_unread(client, monkeypatch):
    h, pid = _setup(client, monkeypatch)
    rep = _scan(client, h, pid, "c1", {"CODEOWNERS": OWNERS})
    assert rep["clean"] is False
    assert any(s["path"].startswith("branch protection") for s in rep["sources_unread"])
