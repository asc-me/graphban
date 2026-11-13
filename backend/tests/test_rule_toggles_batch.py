"""GRPH-1594: `PUT /api/rules/toggles` — several project toggle writes under one reason,
all-or-nothing.

- Every rule in the batch is applied, each with its own `set_rule_toggle` event carrying the
  shared reason.
- One refusal (org lock, bad param, unknown rule) rolls the whole batch back: every rule is
  left as it was, no event is written, and the response names the rule it refused.
- The single-rule PUT stays.
"""
import pytest

from app.services import rule_toggles as T
from app.services import rules as R

SEED_PW = "graphban"
A = "review.sabotage_required"
B = "merge.test_scope_x"


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _db():
    from app.db import SessionLocal
    return SessionLocal()


@pytest.fixture()
def library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    enf = R.Enforcer("graphban_source", "app/services/fleet.py")
    T.register(T.LibraryRule(
        id=A, stage="review", title="Sabotage required", summary="s", enforcement="enforced",
        enforcer=enf, default_on=False, default_params={"min_tests_failed": 1}))
    T.register(T.LibraryRule(
        id=B, stage="merge", title="Scope", summary="s", enforcement="enforced",
        enforcer=enf, default_on=True, default_params={"limit": 3, "names": ["x"]}))
    return T.LIBRARY


@pytest.fixture()
def org(client, monkeypatch, library):
    from app.config import settings
    from app.models import Organization

    monkeypatch.setattr(settings, "hosted_mode", True)
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    a = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()
    db = _db()
    try:
        db.get(Organization, o["id"]).plan = "team"
        db.commit()
    finally:
        db.close()
    return {"owner": owner, "id": o["id"], "a": a["id"]}


def _toggles(client, org):
    r = client.get("/api/rules/toggles", params={"project_id": org["a"]}, headers=org["owner"])
    assert r.status_code == 200, r.text
    return {t["rule_id"]: t for t in r.json()["toggles"]}


def _batch(client, org, toggles, reason="tighten review"):
    return client.put("/api/rules/toggles", params={"project_id": org["a"]},
                      json={"toggles": toggles, "reason": reason}, headers=org["owner"])


def _events():
    from app.models import Event
    db = _db()
    try:
        return [(e.target_id, e.meta) for e in db.query(Event).filter(
            Event.action == "set_rule_toggle").order_by(Event.id)]
    finally:
        db.close()


def _lock_org(org, rule_id):
    from sqlalchemy import select
    from app.models import User
    db = _db()
    try:
        owner = db.scalar(select(User).where(User.email == "alex@ascme-labs.com"))
        T.set_org_toggle(db, org["id"], rule_id, {"on": True, "locked": True},
                         actor=owner, reason="regulated")
    finally:
        db.close()


def test_a_batch_applies_every_rule_with_one_event_each_carrying_the_reason(client, org):
    r = _batch(client, org, [{"rule_id": A, "on": True},
                             {"rule_id": B, "params": {"limit": 5}}])
    assert r.status_code == 200, r.text
    out = r.json()["toggles"]
    assert [t["rule_id"] for t in out] == [A, B]
    assert out[0]["on"] == {"value": True, "source": "project"}
    # The params merge (GRPH-1593) holds in a batch too: `names` is kept.
    assert out[1]["params"]["value"] == {"limit": 5, "names": ["x"]}

    t = _toggles(client, org)
    assert t[A]["on"]["value"] is True and t[B]["params"]["value"]["limit"] == 5
    ev = _events()
    assert [rid for rid, _ in ev] == [A, B]
    assert all(m["reason"] == "tighten review" and m["scope"] == "project" for _, m in ev)


def test_an_org_lock_on_one_rule_rolls_back_the_whole_batch(client, org):
    from app.models import Event

    _lock_org(org, B)
    n_events = len(_events())
    r = _batch(client, org, [{"rule_id": A, "on": True}, {"rule_id": B, "on": False}])
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["detail"].startswith(f"{B}: ") and "locked by the org" in body["detail"]
    assert body["rule_id"] == T.LOCK_RULE
    # Nothing applied: A, written first, is back to the platform default.
    t = _toggles(client, org)
    assert t[A]["on"] == {"value": False, "source": "platform default"}
    assert len(_events()) == n_events
    db = _db()
    try:
        ev = db.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
        assert ev.meta["refused_rule"] == B and ev.meta["locked_rule"] == B
    finally:
        db.close()


def test_a_bad_param_on_a_later_rule_leaves_earlier_rules_unchanged(client, org):
    assert _batch(client, org, [{"rule_id": A, "on": True}]).status_code == 200
    n_events = len(_events())
    r = _batch(client, org, [{"rule_id": A, "on": False},
                             {"rule_id": B, "params": {"limit": "lots"}}])
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith(f"{B}: ") and "'limit' must be int" in r.json()["detail"]
    assert _toggles(client, org)[A]["on"] == {"value": True, "source": "project"}
    assert len(_events()) == n_events


def test_an_unknown_rule_names_itself_and_applies_nothing(client, org):
    r = _batch(client, org, [{"rule_id": A, "on": True}, {"rule_id": "no.such", "on": True}])
    assert r.status_code == 404, r.text
    assert r.json()["detail"].startswith("no.such: ")
    assert _toggles(client, org)[A]["on"]["source"] == "platform default"
    assert _events() == []


@pytest.mark.parametrize("payload, status", [
    ({"toggles": [{"rule_id": A, "on": True}], "reason": " "}, 422),
    ({"toggles": [], "reason": "why"}, 422),
    ({"toggles": [{"rule_id": A, "on": True}, {"rule_id": A, "on": False}], "reason": "w"}, 422),
    ({"toggles": [{"rule_id": A}], "reason": "why"}, 422),
    ({"toggles": [{"rule_id": A, "locked": True}], "reason": "why"}, 422),
])
def test_malformed_batches_are_refused_whole(client, org, payload, status):
    r = client.put("/api/rules/toggles", params={"project_id": org["a"]}, json=payload,
                   headers=org["owner"])
    assert r.status_code == status, r.text
    assert _events() == []


def test_the_single_rule_put_stays(client, org):
    r = client.put(f"/api/rules/toggles/{A}", params={"project_id": org["a"]},
                   json={"on": True, "reason": "one"}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["on"] == {"value": True, "source": "project"}


def test_an_agent_key_cannot_write_a_batch(client, org):
    from app.errors import Refused
    from app.models import ApiKey
    db = _db()
    try:
        with pytest.raises(Refused) as e:
            T.set_project_toggles(db, org["a"], [{"rule_id": A, "on": True}],
                                  actor=ApiKey(id="k"), reason="agent")
        assert e.value.rule_id == T.PERSON_RULE
    finally:
        db.close()
    assert _events() == []
