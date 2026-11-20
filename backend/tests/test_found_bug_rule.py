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


def _line(db):
    from app.services import lines as lines_svc
    return lines_svc.get_line(db, "core") or lines_svc.create_line(db, "core")


def _file_fix(db, held, tps):
    """An agent filing a fix while it holds `held` — the create_item path the MCP tool takes."""
    return items_svc.create_item(
        db, title="bug", kind="fix", description=ACC, touchpoints=tps, project_id="core",
        line_id=_line(db).id, agent_create=True, held=[held])


def test_fix_in_build_refuses_an_overlapping_fix_on_create(db, monkeypatch):
    from app.errors import Refused
    _mode(monkeypatch, "fix_in_build")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    with pytest.raises(Refused) as e:
        _file_fix(db, held, ["backend/app/x.py"])
    assert e.value.status_code == 409 and e.value.rule_id == RULE
    assert held.key in e.value.detail and RULE in e.value.detail
    db.rollback()
    assert _file_fix(db, held, ["backend/app/other.py"]).kind == "fix"   # outside: a ticket


def test_file_ticket_files_the_overlapping_fix(db, monkeypatch):
    _mode(monkeypatch, "file_ticket")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    fix = _file_fix(db, held, ["backend/app/x.py"])
    assert items_svc.found_during_of(db, fix) == held.key


def _to_review(db, item):
    return items_svc.update_item(db, item.id, status="review")


def test_file_ticket_refuses_review_while_diff_touches_open_fix(db, monkeypatch):
    from app.errors import Refused
    _mode(monkeypatch, "file_ticket")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    fix = _file_fix(db, held, ["backend/app/x.py"])
    with pytest.raises(Refused) as e:
        _to_review(db, held)
    assert e.value.status_code == 409 and e.value.rule_id == RULE
    assert fix.key in e.value.detail and RULE in e.value.detail
    db.rollback()
    db.refresh(fix)
    fix.status = "done"
    db.commit()
    assert _to_review(db, held).status == "review"


def test_file_ticket_review_passes_once_the_diff_leaves_the_fix_paths(db, monkeypatch):
    _mode(monkeypatch, "file_ticket")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    _file_fix(db, held, ["backend/app/x.py"])
    held.touchpoints = ["backend/app/y.py"]
    db.commit()
    assert _to_review(db, held).status == "review"


def test_fix_in_build_does_not_refuse_review(db, monkeypatch):
    _mode(monkeypatch, "file_ticket")
    held = _item(db, "held", ["backend/app/x.py"], status="in_progress")
    _file_fix(db, held, ["backend/app/x.py"])
    _mode(monkeypatch, "fix_in_build")
    assert _to_review(db, held).status == "review"


def test_an_agent_key_cannot_write_the_toggle(client, library):
    from app.errors import Refused
    from app.models import ApiKey
    auth = _login(client)
    key = client.post("/api/api-keys", json={"name": "k", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    r = client.put(f"/api/rules/toggles/{RULE}", params={"project_id": "core"},
                   json={"params": {"mode": "file_ticket"}, "reason": "r"},
                   headers={"X-API-Key": key})
    assert r.status_code == 401
    from app.db import SessionLocal
    with SessionLocal() as s:
        k = s.query(ApiKey).first()
        with pytest.raises(Refused) as e:
            T.set_project_toggle(s, "core", RULE, {"params": {"mode": "file_ticket"}},
                                 actor=k, reason="r")
        assert e.value.rule_id == T.PERSON_RULE
        assert T.found_bug_mode(s, "core") == "fix_in_build"


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
        {"name": "mode", "required": False, "type": "str", "comparator": None}]
    assert RULE in R.LIBRARY_BOUND_RULE_IDS
