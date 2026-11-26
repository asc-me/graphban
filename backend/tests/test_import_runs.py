"""P92 D3 (GRPH-1951): the import run record, its report, and batch adoption."""
from app.db import SessionLocal
from app.models import Event

OWNER = "alex@ascme-labs.com"


def _setup(client, monkeypatch):
    from app.services import rule_proposals as RP
    monkeypatch.setattr(RP, "FILE_READER", lambda repo, path, commit: True)
    r = client.post("/api/auth/login", json={"email": OWNER, "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=h).json()["id"]
    return h, pid


def _cite(path):
    return [{"kind": "file", "ref": {"path": path, "commit": "abc123", "lines": "1-3"}}]


def _body():
    src = lambda p: {"path": p, "url": f"https://github.com/o/r/blob/abc123/{p}"}  # noqa: E731
    return {
        "commit": "abc123",
        "sources_read": [{"path": "CODEOWNERS"}],
        "sources_unread": [{"path": "docs/adr/1.md", "reason": "missing permission"}],
        "findings": [
            {"bucket": "proposed", "stage": "Plan", "quote": "migrations are serialised",
             "source": src("CODEOWNERS"),
             "proposal": {"kind": "new_rule", "slug": "areas", "primitive": "exclusive_areas",
                          "title": "Areas", "why": "migrations clash",
                          "params": {"paths": ["db/migrations"]},
                          "citations": _cite("CODEOWNERS")}},
            {"bucket": "proposed", "stage": "Plan", "quote": "vendor is generated",
             "source": src("CODEOWNERS"),
             "proposal": {"kind": "new_rule", "slug": "vendor-areas", "primitive": "exclusive_areas",
                          "title": "Vendor", "why": "vendor clashes",
                          "params": {"paths": ["vendor"]},
                          "citations": _cite("CODEOWNERS")}},
            {"bucket": "external", "stage": "Merge", "quote": "branch protection requires CI",
             "source": src("CONTRIBUTING.md"), "text": "enforced by GitHub"},
            {"bucket": "gap", "stage": "Plan", "quote": "keep modules small",
             "source": src("CONTRIBUTING.md"), "label": "module size",
             "text": "keep modules small", "citations": _cite("CONTRIBUTING.md")},
        ],
    }


def test_run_report_and_batch_adopt(client, monkeypatch):
    h, pid = _setup(client, monkeypatch)
    base = f"/api/projects/{pid}/rules/import-runs"
    r = client.post(base, json=_body(), headers=h)
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["commit"] == "abc123" and rep["sources_unread"][0]["path"] == "docs/adr/1.md"
    assert {f["bucket"] for f in rep["findings"]} == {"proposed", "external", "gap"}
    props = [f for f in rep["findings"] if f["bucket"] == "proposed"]
    assert len(props) == 2
    prop = props[0]
    assert all(f["proposal"]["proposer"] == f"import:{rep['id']}" for f in props)
    assert prop["proposal"]["proposer"] == f"import:{rep['id']}"
    assert prop["proposal"]["proposer_kind"] == "import"
    assert client.get(f"{base}/{rep['id']}", headers=h).json()["id"] == rep["id"]
    assert client.get(base, headers=h).json()[0]["unread"] == 1

    # an external finding has no proposal to adopt
    ext = next(f for f in rep["findings"] if f["bucket"] == "external")
    bad = client.post(f"{base}/{rep['id']}/adopt", json={"finding_ids": [ext["id"]],
                      "reason": "ok"}, headers=h)
    assert bad.status_code == 422

    # Acceptance: adopting the batch with one reason gives every rule a history entry naming
    # the run, and all start `observing`.
    ok = client.post(f"{base}/{rep['id']}/adopt",
                     json={"finding_ids": [f["id"] for f in props],
                           "reason": "matches our repo"}, headers=h)
    assert ok.status_code == 200, ok.text
    assert [p["status"] for p in ok.json()] == ["adopted", "adopted"]
    assert {p["decided_reason"] for p in ok.json()} == {"matches our repo"}
    db = SessionLocal()
    try:
        ev = [e for e in db.query(Event).filter(Event.action == "adopt_rule_proposal")
              if e.project_id == pid]
        assert len(ev) == 2
        for e in ev:
            assert e.meta["import_run"] == str(rep["id"])
            assert f"proposed by import:{rep['id']}" in e.meta["history"]
            assert e.meta["state"] == "observing"
            assert e.meta["batch"] is True
    finally:
        db.close()
    after = client.get(f"{base}/{rep['id']}", headers=h).json()
    assert {f["proposal"]["status"] for f in after["findings"] if f["proposal"]} == {"adopted"}


def test_run_refuses_unknown_bucket_and_uncited(client, monkeypatch):
    h, pid = _setup(client, monkeypatch)
    base = f"/api/projects/{pid}/rules/import-runs"
    b = _body()
    b["findings"][0]["bucket"] = "nope"
    assert client.post(base, json=b, headers=h).status_code == 422
    assert client.get(base, headers=h).json() == []
    b = _body()
    b["findings"][0]["source"] = {}
    assert client.post(base, json=b, headers=h).status_code == 422
    assert client.get(base, headers=h).json() == []


def test_refused_finding_is_kept_on_the_run_and_cannot_be_adopted(client, monkeypatch):
    """The pipeline refuses an uncited proposal; the run still records it, with the refusal,
    and the rest of the run's findings are filed."""
    h, pid = _setup(client, monkeypatch)
    base = f"/api/projects/{pid}/rules/import-runs"
    b = _body()
    b["findings"][0]["proposal"]["citations"] = []
    r = client.post(base, json=b, headers=h)
    assert r.status_code == 201, r.text
    rep = r.json()
    bad = next(f for f in rep["findings"] if f["quote"] == "migrations are serialised")
    assert bad["proposal_id"] is None and bad["refused"]
    good = next(f for f in rep["findings"] if f["quote"] == "vendor is generated")
    assert good["proposal"]["proposer"] == f"import:{rep['id']}" and good["refused"] is None
    gap = next(f for f in rep["findings"] if f["bucket"] == "gap")
    assert gap["guidance_id"] is not None
    nope = client.post(f"{base}/{rep['id']}/adopt", json={"finding_ids": [bad["id"]],
                       "reason": "ok"}, headers=h)
    assert nope.status_code == 422 and "refused" in nope.text
