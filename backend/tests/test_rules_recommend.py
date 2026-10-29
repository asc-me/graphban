"""Recommended rules backend (GRPH-1160)."""
import pytest

from app.db import SessionLocal
from app.models import CodeNode
from app.services import platform as platform_svc
from app.services import rules_recommend as rr

PID = "core"


@pytest.fixture(autouse=True)
def _fresh(client):
    rr.clear_cache()
    yield
    rr.clear_cache()


@pytest.fixture()
def db(client):
    s = SessionLocal()
    yield s
    s.close()


def _rec(client, auth):
    r = client.get(f"/api/rules/recommended?project_id={PID}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(body):
    return {s["id"] for s in body["suggestions"]}


def _state(body, name):
    return next(s for s in body["sources"] if s["source"] == name)


def _add_release_doc(db, path="docs/release.md"):
    db.add(CodeNode(id="cn_rel_" + path.replace("/", "_"), project_id=PID, path=path,
                    kind="file", name="release"))
    db.commit()


def _connect_github(db, monkeypatch, titles):
    platform_svc.connect_github(db, PID, account="acme", repo="acme/app")
    monkeypatch.setattr(rr, "_fetch_pr_titles", lambda repo: titles)
    rr.clear_cache()


def test_sources_always_listed(client, auth):
    body = _rec(client, auth)
    assert [s["source"] for s in body["sources"]] == list(rr.SOURCES)
    assert body["suggestions"] == [] or all(s["evidence"] for s in body["suggestions"])


def test_github_disconnected_reports_not_connected_and_no_pr_suggestion(client, auth, db):
    platform_svc.disconnect_github(db, PID)
    body = _rec(client, auth)
    gh = _state(body, "github")
    assert gh["state"] == "not_available" and gh["reason"] == "not_connected"
    assert "suggest.pr_title_pattern" not in _ids(body)


def test_github_connected_produces_pr_title_suggestion(client, auth, db, monkeypatch):
    _connect_github(db, monkeypatch, ["GRPH-1: a", "GRPH-2: b", "GRPH-3: c", "fix typo"])
    body = _rec(client, auth)
    assert _state(body, "github")["state"] == "read"
    s = next(s for s in body["suggestions"] if s["id"] == "suggest.pr_title_pattern")
    assert s["kind"] == "value" and s["level"] == "advisory" and s["evidence"]
    assert "enforce" not in (s["why"] + s["title"]).lower().replace("never", "")


def test_github_fetch_failure_is_reported(client, auth, db, monkeypatch):
    platform_svc.connect_github(db, PID, account="acme", repo="acme/app")

    def boom(repo):
        raise RuntimeError("down")

    monkeypatch.setattr(rr, "_fetch_pr_titles", boom)
    gh = _state(_rec(client, auth), "github")
    assert gh["state"] == "not_available" and gh["reason"] == "fetch_failed"


def test_empty_code_map_is_reported_not_silent(client, auth, db):
    db.query(CodeNode).filter(CodeNode.project_id == PID).delete()
    db.commit()
    body = _rec(client, auth)
    assert _state(body, "code_graph")["reason"] == "no_code_map"
    assert _state(body, "stack")["reason"] == "no_code_map"


def test_dismissal_survives_cache_expiry_and_returns_on_new_evidence(client, auth, db):
    db.query(CodeNode).filter(CodeNode.project_id == PID).delete()
    db.commit()
    _add_release_doc(db, "docs/release.md")
    assert "suggest.release_defined_in" in _ids(_rec(client, auth))
    r = client.post(f"/api/rules/recommended/suggest.release_defined_in/dismiss?project_id={PID}",
                    headers=auth)
    assert r.status_code == 204
    assert "suggest.release_defined_in" not in _ids(_rec(client, auth))
    rr.clear_cache()  # cache expiry: recomputed from scratch, still dismissed
    assert "suggest.release_defined_in" not in _ids(_rec(client, auth))
    # Evidence changes (the doc moved): it comes back.
    db.query(CodeNode).filter(CodeNode.project_id == PID).delete()
    db.commit()
    _add_release_doc(db, "RELEASE.md")
    rr.clear_cache()
    assert "suggest.release_defined_in" in _ids(_rec(client, auth))


def test_dismissal_is_per_project(client, auth, db, decoy):
    _add_release_doc(db)
    _rec(client, auth)
    client.post(f"/api/rules/recommended/suggest.release_defined_in/dismiss?project_id={PID}",
                headers=auth)
    from app.models import RuleSuggestionDismissal
    rows = db.query(RuleSuggestionDismissal).all()
    assert {r.project_id for r in rows} == {PID}


def test_value_suggestion_disappears_once_field_set(client, auth, db):
    _add_release_doc(db)
    assert "suggest.release_defined_in" in _ids(_rec(client, auth))
    from app.models import Project
    project = db.get(Project, PID)
    project.gitops_release_defined_in = "docs/release.md"
    db.commit()
    assert "suggest.release_defined_in" not in _ids(_rec(client, auth))  # cache still warm


def test_dismiss_unknown_is_404(client, auth):
    r = client.post(f"/api/rules/recommended/nope/dismiss?project_id={PID}", headers=auth)
    assert r.status_code == 404


def test_reading_never_writes_a_rule(client, auth, db):
    before = client.get(f"/api/rules?project_id={PID}", headers=auth).json()
    _rec(client, auth)
    assert client.get(f"/api/rules?project_id={PID}", headers=auth).json() == before
