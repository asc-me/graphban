"""GRPH-1939: a person removes a custom rule (P91 D3, D5).

The observing nudge asks a project admin to "promote or remove the rule"; removal is a person's
act on the Rules page with a reason. The removed rule holds and records nothing after, and its
would-have rows and history stay (D5: removal never purges them)."""
import pytest

from app.errors import NotFound
from app.models import RuleObservation
from app.services import custom_rules as C
from app.services import rule_toggles as T
from tests.test_custom_rules_enforced import (P, RID, _claim, _items, _promote,  # noqa: F401
                                              _propose_and_adopt, box)
from tests.test_rule_proposals import env  # noqa: F401


def _remove(db, headers=None, reason="the walk is over"):
    return db.client.post(f"/api/projects/{db.pid}/rules/custom/{RID}/remove",
                          headers=headers or db.env["h"], json={"reason": reason})


def _listed(db) -> list[str]:
    r = db.client.get(f"/api/projects/{db.pid}/rules/custom", headers=db.env["h"])
    assert r.status_code == 200, r.text
    return [x["rule_id"] for x in r.json()]


def _events(db, action):
    r = db.client.get(f"/api/events?project_id={db.pid}&action={action}&target_type=rule"
                      f"&target_id={RID}", headers=db.env["h"])
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_removed_observing_rule_records_nothing_and_keeps_its_would_have(box):
    db = box
    _propose_and_adopt(db)
    _items(db)
    assert _claim(db, "GRPH-A1")["claimed"]
    assert _claim(db, "GRPH-A2")["claimed"]
    before = db.query(RuleObservation).filter_by(rule_id=RID).count()
    assert before == 1
    assert RID in _listed(db)

    r = _remove(db)
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True and r.json()["state"] == "observing"
    db.expire_all()
    assert RID not in _listed(db)
    with pytest.raises(NotFound):
        C.get(db, RID, db.pid)
    assert RID not in [t["rule_id"] for t in T.effective(db, db.pid)]

    # D5: the would-have record stays readable after removal ...
    h = db.client.get(f"/api/projects/{db.pid}/rules/{RID}/observations", headers=db.env["h"])
    assert h.status_code == 200 and len(h.json()) == before, h.text
    # ... and the rule records nothing new: the same clash again adds no row.
    for t in ("three", "four"):
        from app.services import items as items_svc
        items_svc.create_item(db, title=t, project_id=db.pid, touchpoints=[P + f"/{t}.md"],
                              status="next")
    assert _claim(db, "GRPH-A3")["claimed"]
    assert _claim(db, "GRPH-A4")["claimed"]
    assert db.query(RuleObservation).filter_by(rule_id=RID).count() == before

    ev = _events(db, "remove_custom_rule")
    assert len(ev) == 1 and ev[0]["meta"]["reason"] == "the walk is over"
    assert ev[0]["meta"]["removed"]["state"] == "observing"
    assert ev[0]["meta"]["would_hold"] == 1
    # the adoption that started it is history too, and stays
    assert _events(db, "adopt_rule_proposal")


def test_removed_active_rule_holds_nothing(box):
    db = box
    _propose_and_adopt(db)
    _promote(db)
    _items(db)
    assert _claim(db, "GRPH-A1")["claimed"]
    assert not _claim(db, "GRPH-A2")["claimed"]  # held while active
    assert _remove(db).status_code == 200
    db.expire_all()
    second = _claim(db, "GRPH-A2")
    assert second["claimed"], second  # the removed rule holds nothing
    assert RID not in _listed(db)
    assert _events(db, "promote_rule")  # the promotion's history stays


def test_an_agent_key_is_refused_naming_person_adopts(box):
    db = box
    _propose_and_adopt(db)
    r = _remove(db, headers={"X-API-Key": db.env["key"]})
    assert r.status_code == 403, r.text
    assert T.PERSON_RULE in r.text
    db.expire_all()
    assert C.get(db, RID, db.pid).state == "observing"


def test_remove_needs_a_reason_and_a_custom_rule(box):
    db = box
    _propose_and_adopt(db)
    assert _remove(db, reason="  ").status_code == 422
    r = db.client.post(f"/api/projects/{db.pid}/rules/custom/custom.nope/remove",
                       headers=db.env["h"], json={"reason": "x"})
    assert r.status_code == 404, r.text
    r = db.client.post(f"/api/projects/{db.pid}/rules/custom/{T.EXCLUSIVE_AREAS_RULE}/remove",
                       headers=db.env["h"], json={"reason": "x"})
    assert r.status_code == 422 and "toggle" in r.text
    assert RID in _listed(db)
