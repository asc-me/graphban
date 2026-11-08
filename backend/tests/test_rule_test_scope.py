"""GRPH-1405: the `build.test_scope` rule — where the test suites run, a project decision."""
from __future__ import annotations

import pytest

from app.services import delegation as D
from app.services import rule_toggles as T
from app.services import rules as R

RULE = T.TEST_SCOPE_RULE
MODES = ("agent_full", "agent_and_pr", "agent_targeted")


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="build", title="Where the test suites run",
        summary="Which of the builder and the PR's CI run the full suites.",
        enforcement="checked", enforcer=R.Enforcer("doc", "docs/rules.md"),
        default_on=True, default_params={"mode": T.TEST_SCOPE_DEFAULT}))
    return T.LIBRARY


def _put(client, auth, body, project="core"):
    return client.put(f"/api/rules/toggles/{RULE}", params={"project_id": project},
                      json=body, headers=auth)


def _mode(client, auth, project="core"):
    r = client.get("/api/rules/toggles", params={"project_id": project}, headers=auth)
    t = next(t for t in r.json()["toggles"] if t["rule_id"] == RULE)
    return t["params"]["value"]["mode"], t["params"]["source"]


def test_unset_project_reads_agent_and_pr(client, library):
    assert _mode(client, _login(client)) == ("agent_and_pr", "platform default")


def test_choosing_a_mode_persists_and_a_reload_shows_it(client, library):
    auth = _login(client)
    for mode in MODES:
        r = _put(client, auth, {"params": {"mode": mode}, "reason": f"try {mode}"})
        assert r.status_code == 200, r.text
        assert _mode(client, auth) == (mode, "project")


def test_a_mode_outside_the_three_is_refused(client, library):
    auth = _login(client)
    r = _put(client, auth, {"params": {"mode": "nobody_runs_ci"}, "reason": "x"})
    assert r.status_code == 422 and "agent_full" in r.text
    assert _mode(client, auth)[0] == "agent_and_pr"


def test_the_change_is_in_the_rule_history(client, library):
    from app.db import SessionLocal
    from app.models import Event

    auth = _login(client)
    assert _put(client, auth, {"params": {"mode": "agent_targeted"}, "reason": "CI is the gate"}
                ).status_code == 200
    db = SessionLocal()
    try:
        ev = [e for e in db.query(Event).filter(Event.action == "set_rule_toggle",
                                                Event.target_id == RULE).order_by(Event.id)][-1]
    finally:
        db.close()
    assert ev.meta["reason"] == "CI is the gate"
    assert ev.meta["new"]["params"] == {"mode": "agent_targeted"}


def test_service_default_without_the_rule_in_the_library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    assert T.test_scope_mode(None, "core") == "agent_and_pr"


def test_brief_names_the_mode_and_changes_with_it(client, library):
    from app.db import SessionLocal
    from app.models import Item

    auth = _login(client)
    db = SessionLocal()
    try:
        item = db.query(Item).filter(Item.project_id == "core").first()
        seen = {}
        for mode in MODES:
            assert _put(client, auth, {"params": {"mode": mode}, "reason": "r"}).status_code == 200
            b = D.brief(db, item)
            seen[mode] = next(ln for ln in b["text"].splitlines() if ln.startswith("Test scope:"))
            assert b["test_scope"] == mode and mode in seen[mode]
        assert len(set(seen.values())) == 3
    finally:
        db.close()


def test_every_mode_has_a_brief_line_and_a_label():
    assert set(D._TEST_SCOPE_BRIEF) == set(T.TEST_SCOPE_MODES) == set(MODES)
