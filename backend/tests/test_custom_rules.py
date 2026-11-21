"""P91 S2 (GRPH-1774): custom rules stored per project, resolved through the toggle layering."""
import pytest

from app.services import custom_rules as C
from app.services import rule_pack as rp
from app.services import rule_toggles as T

SEED_PW = "graphban"
BODY = {"slug": "no-migration-clash", "primitive": "exclusive_areas", "title": "No clash",
        "why": "Two agents must not touch migrations at once.",
        "params": {"paths": ["backend/alembic/versions"]}, "reason": "we lost a day to this"}


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def two(client):
    owner = _login(client, "alex@ascme-labs.com")
    a = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()["id"]
    b = client.post("/api/projects", json={"name": "Probe"}, headers=owner).json()["id"]
    return {"h": owner, "a": a, "b": b}


def _post(client, two, pid, body=BODY, headers=None):
    return client.post(f"/api/projects/{pid}/rules/custom", json=body,
                       headers=headers or two["h"])


def _db():
    from app.db import SessionLocal
    return SessionLocal()


def test_person_saves_a_custom_rule_and_it_resolves_as_custom(client, two):
    r = _post(client, two, two["a"])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["rule_id"] == "custom.no-migration-clash" and out["state"] == "active"
    assert out["stage"] == "Plan" and out["author"] and out["why"] == BODY["why"]
    db = _db()
    try:
        s = T.resolve(db, "custom.no-migration-clash", two["a"])
        assert s["on"] == {"value": True, "source": "custom"}
        assert s["params"]["source"] == "custom"
        assert s["params"]["value"] == {"paths": ["backend/alembic/versions"]}
        assert s["unmeasured_reason"] is None
    finally:
        db.close()
    listed = client.get(f"/api/projects/{two['a']}/rules/custom", headers=two["h"]).json()
    assert [x["rule_id"] for x in listed] == ["custom.no-migration-clash"]
    toggles = {t["rule_id"]: t for t in client.get(
        "/api/rules/toggles", params={"project_id": two["a"]}, headers=two["h"]).json()["toggles"]}
    assert toggles["custom.no-migration-clash"]["on"]["source"] == "custom"
    reg = {x["id"]: x for x in client.get(
        "/api/rules", params={"project_id": two["a"]}, headers=two["h"]).json()["rules"]}
    assert reg["custom.no-migration-clash"]["source"] == "custom"


def test_duplicate_slug_refused_same_slug_in_other_project_allowed(client, two):
    assert _post(client, two, two["a"]).status_code == 201
    dup = _post(client, two, two["a"])
    assert dup.status_code == 422 and "already exists" in dup.text
    assert _post(client, two, two["b"]).status_code == 201


def test_invalid_drafts_are_refused_by_the_shared_validator(client, two):
    bad = _post(client, two, two["a"], {**BODY, "params": {"nope": 1}})
    assert bad.status_code == 422 and "not a param" in bad.text
    assert _post(client, two, two["a"], {**BODY, "primitive": "zzz"}).status_code == 422
    assert _post(client, two, two["a"], {**BODY, "slug": "Bad Slug"}).status_code == 422
    assert _post(client, two, two["a"], {**BODY, "reason": ""}).status_code == 422


def test_primitive_api_major_bump_draws_unmeasured(client, two, monkeypatch):
    assert _post(client, two, two["a"]).status_code == 201
    monkeypatch.setattr(rp, "PRIMITIVE_API", "2.0.0")
    db = _db()
    try:
        s = T.resolve(db, "custom.no-migration-clash", two["a"])
        assert s["on"]["value"] is False
        assert "major mismatch" in s["unmeasured_reason"]
    finally:
        db.close()
    reg = {x["id"]: x for x in client.get(
        "/api/rules", params={"project_id": two["a"]}, headers=two["h"]).json()["rules"]}
    r = reg["custom.no-migration-clash"]
    assert r["enforcement"] == "unmeasured" and "major mismatch" in r["unmeasured_reason"]


def test_primitive_gone_draws_unmeasured(client, two, monkeypatch):
    assert _post(client, two, two["a"]).status_code == 201
    monkeypatch.delitem(rp.PRIMITIVES, "exclusive_areas")
    db = _db()
    try:
        s = T.resolve(db, "custom.no-migration-clash", two["a"])
        assert s["on"]["value"] is False and "does not exist" in s["unmeasured_reason"]
    finally:
        db.close()


def test_agent_key_cannot_write_a_custom_rule(client, two):
    key = client.post("/api/api-keys", json={"name": "k", "project_id": two["a"]},
                      headers=two["h"]).json()["plaintext"]
    r = _post(client, two, two["a"], headers={"X-API-Key": key})
    assert r.status_code == 403 and T.PERSON_RULE in r.text + str(r.headers)
    from app.models import CustomRule
    db = _db()
    try:
        assert db.query(CustomRule).count() == 0
    finally:
        db.close()


def test_custom_rule_is_invisible_across_projects(client, two):
    assert _post(client, two, two["a"]).status_code == 201
    assert client.get(f"/api/projects/{two['b']}/rules/custom",
                      headers=two["h"]).json() == []
    db = _db()
    try:
        from app.errors import NotFound
        with pytest.raises(NotFound):
            T.resolve(db, "custom.no-migration-clash", two["b"])
    finally:
        db.close()


def test_stranger_cannot_read_or_write(client, two):
    # dana is not a member of alex's project: 404, never the rule (cross-tenant probe).
    assert _post(client, two, two["a"]).status_code == 201
    h = _login(client, "dana@ascme-labs.com")
    assert client.get(f"/api/projects/{two['a']}/rules/custom", headers=h).status_code == 404
    other = dict(BODY, slug="intruder")
    assert _post(client, two, two["a"], other, headers=h).status_code == 404


def test_service_refuses_a_non_person(client, two):
    from app.errors import Refused
    db = _db()
    try:
        with pytest.raises(Refused) as e:
            C.create(db, ("system", "x", "x"), two["a"], slug="x", primitive="exclusive_areas",
                     title="t", why="w", reason="r", params={"paths": ["a"]})
        assert e.value.rule_id == T.PERSON_RULE
    finally:
        db.close()


def test_underscore_slug_refused_naming_the_hyphen_rule(client, two):
    r = _post(client, two, two["a"], {**BODY, "slug": "no_clash"})
    assert r.status_code == 422 and "hyphens only" in r.text
    assert _post(client, two, two["a"], {**BODY, "slug": "no-clash"}).status_code == 201


def test_slug_regex_is_hyphen_only_and_preexisting_underscore_rule_still_renders(client, two):
    assert not C.SLUG.match("a_b") and C.SLUG.match("a-b")
    db = _db()
    try:
        row = C.CustomRule(rule_id="custom.old_rule", project_id=two["a"], primitive="exclusive_areas",
                           primitive_api=rp.PRIMITIVE_API, stage="Plan", params={"paths": ["x"]},
                           title="Old", why="w", claims="enforced", state="active",
                           author_id=None, reason="r")
        db.add(row)
        db.commit()
    finally:
        db.close()
    listed = client.get(f"/api/projects/{two['a']}/rules/custom", headers=two["h"]).json()
    assert "custom.old_rule" in [x["rule_id"] for x in listed]
