"""A project can be archived and unarchived (GRPH-1955).

A finished walk or test project (p74-walk, p91-walk) used to stay in the picker, in
`list_projects`, and in planner and drain reads forever, with its rules still evaluating.
Archive is the soft, reversible fix: a delete would cascade through items, events and
attestations, and free a tag the retag invariant says is never reclaimed.
"""
import pytest

from app.services import projects as projects_svc

SEED_PW = "graphban"


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": SEED_PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _db():
    from app.db import SessionLocal
    return SessionLocal()


@pytest.fixture()
def walk(client, auth):
    """A fresh project owned by alex, holding one ready item."""
    r = client.post("/api/projects", json={"name": "P91 walk"}, headers=auth)
    assert r.status_code == 201, r.text
    pid, tag = r.json()["id"], r.json()["tag"]
    r = client.post("/api/items", json={"title": "walk step", "project_id": pid,
                                        "status": "next", "touchpoints": ["walk/a.py"]},
                    headers=auth)
    assert r.status_code == 201, r.text
    return {"pid": pid, "tag": tag, "item": r.json()["id"]}


@pytest.fixture()
def key(client, auth):
    r = client.post("/api/api-keys", json={"name": "agent", "scopes": ["read", "write"]},
                    headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["plaintext"]


def _rpc(client, key, tool, arguments):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _archive(client, auth, pid, reason="walk finished"):
    return client.post(f"/api/projects/{pid}/archive", json={"reason": reason}, headers=auth)


def _refused_archived(result):
    assert result.get("isError") is True, result
    err = result["structuredContent"]["error"]
    assert "archived" in err["message"], err
    return err


# ---- archive / unarchive by a person ---------------------------------------------------------

def test_owner_archives_with_a_reason_and_unarchives(client, auth, walk):
    r = _archive(client, auth, walk["pid"])
    assert r.status_code == 200, r.text
    assert r.json()["archived_at"] is not None
    assert r.json()["archived_reason"] == "walk finished"

    r = client.post(f"/api/projects/{walk['pid']}/unarchive", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["archived_at"] is None and r.json()["archived_reason"] == ""


def test_archive_needs_a_reason(client, auth, walk):
    assert _archive(client, auth, walk["pid"], reason="").status_code == 422
    db = _db()
    try:
        assert not projects_svc.is_archived(db, walk["pid"])
    finally:
        db.close()


def test_a_member_who_is_not_owner_or_admin_is_refused(client, auth, walk):
    from app.models import Membership

    db = _db()
    try:
        db.add(Membership(user_id="u2", project_id=walk["pid"], role="member", access="write"))
        db.commit()
    finally:
        db.close()
    dana = _login(client, "dana@ascme-labs.com")
    r = _archive(client, dana, walk["pid"])
    assert r.status_code == 403, r.text
    assert r.json()["rule_id"] == "project.archive_needs_admin"


def test_archive_and_unarchive_are_recorded_as_events(client, auth, walk):
    from sqlalchemy import select

    from app.models import Event

    _archive(client, auth, walk["pid"])
    client.post(f"/api/projects/{walk['pid']}/unarchive", headers=auth)
    db = _db()
    try:
        actions = list(db.scalars(select(Event.action).where(
            Event.project_id == walk["pid"],
            Event.action.in_(("archive_project", "unarchive_project")))))
    finally:
        db.close()
    assert sorted(actions) == ["archive_project", "unarchive_project"]


# ---- an agent key is refused by name ---------------------------------------------------------

@pytest.mark.parametrize("route", ["archive", "unarchive"])
def test_an_agent_key_gets_a_403_naming_the_person_only_rule(client, key, walk, route):
    for headers in ({"X-API-Key": key}, {"Authorization": f"Bearer {key}"}):
        r = client.post(f"/api/projects/{walk['pid']}/{route}", json={"reason": "agent"},
                        headers=headers)
        assert r.status_code == 403, (headers, r.status_code, r.text)
        assert r.json()["rule_id"] == "project.archive_person_only"
        assert "person" in r.json()["detail"]
    db = _db()
    try:
        assert not projects_svc.is_archived(db, walk["pid"])
    finally:
        db.close()


# ---- lists leave it out; reads by id still work ----------------------------------------------

def test_archived_leaves_the_default_lists_and_include_archived_shows_it(client, auth, key, walk):
    _archive(client, auth, walk["pid"])

    ids = [p["id"] for p in client.get("/api/projects", headers=auth).json()]
    assert walk["pid"] not in ids and "core" in ids
    rows = client.get("/api/projects?include_archived=true", headers=auth).json()
    row = next(p for p in rows if p["id"] == walk["pid"])
    assert row["archived_at"] is not None

    summ = client.get("/api/projects/summaries", headers=auth).json()
    assert walk["pid"] not in [p["id"] for p in summ]
    summ = client.get("/api/projects/summaries?include_archived=true", headers=auth).json()
    assert next(p for p in summ if p["id"] == walk["pid"])["archived_at"] is not None

    listed = _rpc(client, key, "list_projects", {})["structuredContent"]["results"]
    assert walk["pid"] not in [p["id"] for p in listed]
    assert all(p["archived"] is False for p in listed)
    listed = _rpc(client, key, "list_projects",
                  {"include_archived": True})["structuredContent"]["results"]
    assert next(p for p in listed if p["id"] == walk["pid"])["archived"] is True


def test_archived_keeps_its_tag_items_and_history_readable(client, auth, walk):
    _archive(client, auth, walk["pid"])
    assert client.get(f"/api/items/{walk['item']}", headers=auth).status_code == 200
    assert client.get(f"/api/projects/{walk['pid']}/counts", headers=auth).status_code == 200
    # The tag is still held: nothing else may take it while the project is archived.
    check = client.get(f"/api/projects/tag-check?tag={walk['tag']}", headers=auth).json()
    assert check["available"] is False


# ---- writes are refused, naming the archived state -------------------------------------------

def test_claim_refused_on_an_archived_project(client, auth, key, walk):
    _archive(client, auth, walk["pid"])
    err = _refused_archived(_rpc(client, key, "claim_next",
                                 {"project_id": walk["pid"], "agent_id": "walker"}))
    assert err["code"] == "conflict"
    db = _db()
    try:
        from app.models import Item
        assert db.get(Item, walk["item"]).claimed_by is None
    finally:
        db.close()


def test_claim_works_again_after_unarchive(client, auth, key, walk):
    _archive(client, auth, walk["pid"])
    client.post(f"/api/projects/{walk['pid']}/unarchive", headers=auth)
    out = _rpc(client, key, "claim_next", {"project_id": walk["pid"], "agent_id": "walker"})
    assert out.get("isError") is not True, out
    assert out["structuredContent"]["claimed"] is True


def test_claim_cluster_refused_on_an_archived_project(client, auth, key, walk):
    _archive(client, auth, walk["pid"])
    _refused_archived(_rpc(client, key, "claim_cluster",
                           {"project_id": walk["pid"], "agent_id": "walker"}))


def test_create_and_update_item_refused_on_an_archived_project(client, auth, key, walk):
    _archive(client, auth, walk["pid"])
    _refused_archived(_rpc(client, key, "create_item",
                           {"title": "late", "project_id": walk["pid"]}))
    _refused_archived(_rpc(client, key, "update_item", {"id": walk["item"], "title": "renamed"}))
    # The REST paths a person uses are refused the same way.
    r = client.post("/api/items", json={"title": "late", "project_id": walk["pid"]}, headers=auth)
    assert r.status_code == 409 and r.json()["rule_id"] == projects_svc.ARCHIVED_RULE, r.text
    r = client.patch(f"/api/items/{walk['item']}", json={"title": "renamed"}, headers=auth)
    assert r.status_code == 409 and r.json()["rule_id"] == projects_svc.ARCHIVED_RULE, r.text


def test_delegate_refused_on_an_archived_project(client, auth, walk):
    from app.errors import Refused
    from app.models import Item
    from app.services import delegation as delegation_svc

    _archive(client, auth, walk["pid"])
    db = _db()
    try:
        item = db.get(Item, walk["item"])
        with pytest.raises(Refused) as e:
            delegation_svc.delegate(db, agent=None, item=item, lane="backend", tier="cheap",
                                    lease_seconds=600)
        assert e.value.rule_id == projects_svc.ARCHIVED_RULE
        assert "archived" in e.value.detail
    finally:
        db.close()


def test_an_unscoped_sweep_never_offers_an_archived_projects_items(client, auth, walk):
    from app.services import items as items_svc

    _archive(client, auth, walk["pid"])
    db = _db()
    try:
        cands = items_svc._ready_candidates(db, None, 600)
        assert walk["item"] not in [c.id for c in cands]
    finally:
        db.close()


# ---- its custom and adopted rules stop holding or recording ----------------------------------

def test_custom_rules_stop_holding_and_observations_stop_recording(client, auth, walk):
    from app.services import custom_rules, rule_observations, rule_proposals

    body = {"slug": "walk-areas", "primitive": "exclusive_areas", "title": "Walk areas",
            "why": "One walker at a time.", "params": {"paths": ["walk"]}, "reason": "walk"}
    r = client.post(f"/api/projects/{walk['pid']}/rules/custom", json=body, headers=auth)
    assert r.status_code == 201, r.text
    db = _db()
    try:
        active, _ = custom_rules.exclusive_area_rules(db, walk["pid"])
        assert active, "the custom rule should hold before the archive"
    finally:
        db.close()

    _archive(client, auth, walk["pid"])
    db = _db()
    try:
        assert custom_rules.exclusive_area_rules(db, walk["pid"]) == ([], [])
        assert rule_observations.record(db, walk["pid"], "plan.exclusive_areas",
                                        "would_refuse") is None
        assert rule_proposals.adopted_for_stage(db, walk["pid"], "build", 5) == []
    finally:
        db.close()
