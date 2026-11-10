"""GRPH-1497: `memory.write_mode` is a rule, and a trusted project does not draw
`memory.independent_adjudication` as holding."""
import pytest

from app.services import rules as R

SEED_PW = "graphban"
RULE = "memory.write_mode"
ADJ = "memory.independent_adjudication"


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": SEED_PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return _login(client)


@pytest.fixture()
def pid(client, owner):
    return client.post("/api/projects", json={"name": "Mode"}, headers=owner).json()["id"]


def _registry(pid):
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        return {r["id"]: r for r in R.registry(db, pid)}
    finally:
        db.close()


def _history(client, owner, pid):
    r = client.get("/api/events", params={"project_id": pid, "action": "set_rule_toggle",
                                          "target_type": "rule", "target_id": RULE},
                   headers=owner)
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_review_draws_adjudication_as_today_and_trusted_draws_it_not_used(client, owner, pid):
    reg = _registry(pid)
    assert reg[RULE]["value"] == "review" and reg[RULE]["truth"] == "db"
    assert reg[RULE]["stage"] == "learn"
    assert not reg[ADJ].get("not_used_reason") and R.rule_used(reg[ADJ], None)

    r = client.put(f"/api/projects/{pid}/memory-write-mode", json={"mode": "trusted", "reason": "solo"},
                   headers=owner)
    assert r.status_code == 200, r.text
    reg = _registry(pid)
    assert reg[RULE]["value"] == "trusted"
    assert RULE in reg[ADJ]["not_used_reason"] and "without adjudication" in reg[ADJ]["not_used_reason"]
    assert not R.rule_used(reg[ADJ], None)

    flow = client.get("/api/rules/flow", params={"project_id": pid}, headers=owner).json()
    node = next(n for s in flow["stages"] for n in s["rules"] if n["id"] == ADJ)
    assert node["used"] is False and RULE in node["label"]

    client.put(f"/api/projects/{pid}/memory-write-mode", json={"mode": "auto", "reason": "back"},
               headers=owner)
    assert not _registry(pid)[ADJ].get("not_used_reason")


def test_rules_page_change_needs_a_reason_and_is_recorded(client, owner, pid):
    r = client.put(f"/api/projects/{pid}/memory-write-mode", json={"mode": "trusted"}, headers=owner)
    assert r.status_code == 422
    r = client.put(f"/api/projects/{pid}/memory-write-mode", json={"mode": "bogus", "reason": "x"},
                   headers=owner)
    assert r.status_code == 422
    assert _registry(pid)[RULE]["value"] == "review"
    assert _history(client, owner, pid) == []

    r = client.put(f"/api/projects/{pid}/memory-write-mode",
                   json={"mode": "trusted", "reason": "solo project"}, headers=owner)
    assert r.status_code == 200
    (ev,) = _history(client, owner, pid)
    assert ev["actor_type"] == "user" and ev["actor_label"] and ev["ts"]
    assert ev["meta"]["old"] == {"value": "review"} and ev["meta"]["new"] == {"value": "trusted"}
    assert ev["meta"]["reason"] == "solo project"


def test_settings_update_is_in_the_same_history_reason_optional(client, owner, pid):
    r = client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "auto"}, headers=owner)
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/projects/{pid}",
                     json={"memory_write_mode": "trusted", "memory_write_mode_reason": "why"},
                     headers=owner)
    assert r.status_code == 200
    # Re-sending the same mode is not a change.
    client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "trusted"}, headers=owner)
    hist = list(reversed(_history(client, owner, pid)))
    assert [(e["meta"]["old"]["value"], e["meta"]["new"]["value"]) for e in hist] == [
        ("review", "auto"), ("auto", "trusted")]
    assert hist[0]["meta"]["reason"] == "" and hist[1]["meta"]["reason"] == "why"
    assert _registry(pid)[RULE]["value"] == "trusted"


def test_an_agent_cannot_change_the_mode(client, owner, pid):
    from app.db import SessionLocal
    from app.errors import Refused
    from app.models import ApiKey, Project
    from app.services import memory

    key = client.post("/api/api-keys", json={"name": "k", "project_id": pid},
                      headers=owner).json()["plaintext"]
    r = client.put(f"/api/projects/{pid}/memory-write-mode",
                   json={"mode": "trusted", "reason": "r"}, headers={"X-API-Key": key})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        with pytest.raises(Refused) as e:
            memory.set_write_mode(db, db.get(Project, pid), "trusted", actor=db.query(ApiKey).first(),
                                  reason="r")
        assert e.value.rule_id == "rules.person_adopts"
        assert db.get(Project, pid).memory_write_mode == "review"
    finally:
        db.close()
