"""GRPH-1682: the `build.found_bug` rule — mode reader, overlap and review refusals, brief line."""
from __future__ import annotations

import pytest

from app.services import delegation as D
from app.services import items as items_svc
from app.services import rule_toggles as T
from app.services import rules as R

RULE = T.FOUND_BUG_RULE
ACC = "Does it.\n\n## Acceptance\n- the fix works\n"


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="build", title="A bug found while building",
        summary="Fix it on the item, or file a ticket.",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/items.py"),
        default_on=True, default_params={"mode": T.FOUND_BUG_DEFAULT}))
    return T.LIBRARY


def _put(client, auth, body, project="core"):
    return client.put(f"/api/rules/toggles/{RULE}", params={"project_id": project},
                      json=body, headers=auth)


def test_unknown_mode_is_a_422_and_known_ones_persist(client, library):
    auth = _login(client)
    r = _put(client, auth, {"params": {"mode": "ignore_it"}, "reason": "x"})
    assert r.status_code == 422 and "fix_in_build" in r.text
    for mode in ("file_ticket", "fix_in_build"):
        assert _put(client, auth, {"params": {"mode": mode}, "reason": "r"}).status_code == 200


def test_reader_defaults(db, monkeypatch, library):
    assert T.found_bug_mode(db, "core") == "fix_in_build"          # never set
    monkeypatch.setattr(T, "LIBRARY", {})
    assert T.found_bug_mode(db, "core") == "fix_in_build"          # pack has no template


def test_reader_off_and_unknown_stored_mode(db, monkeypatch, library):
    def state(on, mode):
        return {"on": {"value": on}, "params": {"value": {"mode": mode}}}
    monkeypatch.setattr(T, "resolve", lambda *a, **k: state(False, "file_ticket"))
    assert T.found_bug_mode(db, "core") == "fix_in_build"
    for bad in ("", "nonsense", None):
        monkeypatch.setattr(T, "resolve", lambda *a, _b=bad, **k: state(True, _b))
        assert T.found_bug_mode(db, "core") == "fix_in_build"
    monkeypatch.setattr(T, "resolve", lambda *a, **k: state(True, "file_ticket"))
    assert T.found_bug_mode(db, "core") == "file_ticket"


def _mode(monkeypatch, mode):
    monkeypatch.setattr(T, "found_bug_mode", lambda db, pid: mode)


def _item(db, title, tps, **kw):
    return items_svc.create_item(db, title=title, touchpoints=tps, project_id="core", **kw)


def test_fix_in_build_refuses_an_overlapping_fix(db, monkeypatch):
    _mode(monkeypatch, "fix_in_build")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    with pytest.raises(ValueError) as e:
        items_svc.refuse_overlap_with_held(db, touchpoints=["backend/app/x.py"], held=[held],
                                           project_id="core")
    assert held.key in str(e.value) and "build.found_bug" in str(e.value)
    items_svc.refuse_overlap_with_held(db, touchpoints=["backend/app/other.py"], held=[held],
                                       project_id="core")


def test_file_ticket_does_not_refuse_the_overlap(db, monkeypatch):
    _mode(monkeypatch, "file_ticket")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    items_svc.refuse_overlap_with_held(db, touchpoints=["backend/app/x.py"], held=[held],
                                       project_id="core")


def _fix_found_during(db, held, tps, status="backlog"):
    from app.models import Link
    fix = _item(db, "fix", tps, status=status)
    fix.kind = "fix"
    db.add(Link(project_id="core", a=fix.id, b=held.id, type="found_during",
                reason="found during", confidence=1.0))
    db.commit()
    return fix


def test_file_ticket_refuses_review_while_diff_touches_open_fix(db, monkeypatch):
    _mode(monkeypatch, "file_ticket")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    fix = _fix_found_during(db, held, ["backend/app/x.py"])
    with pytest.raises(ValueError) as e:
        items_svc.refuse_review_over_open_fix(db, held)
    assert fix.key in str(e.value) and "build.found_bug" in str(e.value)
    fix.status = "done"
    db.commit()
    items_svc.refuse_review_over_open_fix(db, held)


def test_fix_in_build_does_not_refuse_review(db, monkeypatch):
    _mode(monkeypatch, "fix_in_build")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    _fix_found_during(db, held, ["backend/app/x.py"])
    items_svc.refuse_review_over_open_fix(db, held)


def test_brief_has_one_found_bug_line(db, monkeypatch):
    _mode(monkeypatch, "file_ticket")
    held = _item(db, "held", ["backend/app/x.py"])
    brief = D.brief(db, held)
    lines = [l for l in brief["text"].splitlines() if l.startswith("Found bug:")]
    assert len(lines) == 1 and lines[0].startswith("Found bug: file_ticket")
    assert brief["found_bug"] == "file_ticket"


def test_contract_and_cited_rule():
    import json
    from pathlib import Path
    c = json.loads((Path(__file__).parents[2] / "docs/rule-primitive-contract.json").read_text())
    p = next(x for x in c["primitives"] if x["id"] == "found_bug")
    assert p["level"] == "enforced" and p["params"] == [
        {"name": "mode", "required": False, "type": "str"}]
    assert RULE in R.LIBRARY_BOUND_RULE_IDS
