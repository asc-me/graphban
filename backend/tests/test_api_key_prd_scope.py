"""An API key limited to one PRD (GRPH-1973, P96 "Key").

`gbfleet drain create` mints one key per drain through `POST /api/api-keys` with `prd_id`. A
leaked drain key must not be able to claim other work, reach another project, or keep working
after it is revoked; and the audit trail must name the drain's key, not another of its owner's.

Refusal tests (design philosophy §2): every clause is shown by the wrong key being turned away,
next to a control showing the right key let in, so a refusal cannot pass by refusing everything.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import ApiKey, Event, Item
from app.services import fleet as fleet_svc
from app.services import items as items_svc


def _mcp_raw(client, key, name, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )


def _mcp(client, key, name, args=None):
    r = _mcp_raw(client, key, name, args)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


def _err(res) -> dict:
    assert res.get("isError"), res
    return res["structuredContent"]["error"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Drained"}, headers=auth).json()["id"]


@pytest.fixture()
def other_proj(client, auth):
    return client.post("/api/projects", json={"name": "Elsewhere"}, headers=auth).json()["id"]


@pytest.fixture()
def owner_key(client, auth, proj):
    """The same user's ordinary, unlimited key — the one a PRD key must not act as."""
    return client.post("/api/api-keys", json={"name": "laptop", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _prd(client, auth, proj, title):
    r = client.post("/api/prds", json={"title": title, "project_id": proj}, headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()


@pytest.fixture()
def mine(client, auth, proj):
    return _prd(client, auth, proj, "the drain's PRD")


@pytest.fixture()
def theirs(client, auth, proj):
    return _prd(client, auth, proj, "another PRD")


def _mint(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "drain-a @ host-1", **body}, headers=auth)


@pytest.fixture()
def drain(client, auth, mine):
    r = _mint(client, auth, prd_id=mine["id"])
    assert r.status_code == 201, r.text
    return r.json()


def _item(client, key, title, touchpoints, prd_id=None, status="next"):
    args = {"title": title, "status": status, "touchpoints": touchpoints}
    if prd_id:
        args["prd_id"] = prd_id
    return _ok(_mcp(client, key, "create_item", args))["id"]


def _agent(client, key, label) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label}))["agent_id"]


def _stored(db, item_key) -> Item:
    from app.services import keys

    db.expire_all()
    return db.get(Item, keys.resolve_item(db, item_key) or item_key)


def _to_review(db, item_key, built_by):
    row = _stored(db, item_key)
    row.status, row.built_by, row.branch = "review", built_by, f"gb/{item_key.lower()}"
    row.evidence = [{"kind": "url", "detail": "PR", "url": "https://github.com/o/r/pull/1"}]
    db.commit()


# ---- the key is minted limited -------------------------------------------------------------

def test_the_minted_key_is_pinned_to_the_prds_project_and_carries_fleet_use(
        client, auth, drain, mine, proj, db):
    assert drain["prd_id"] == mine["id"]
    assert drain["project_id"] == proj
    assert "fleet" in (drain["tool_tiers"] or [])
    row = db.get(ApiKey, drain["id"])
    assert row.prd_id == mine["id"]


def test_the_value_is_returned_once_and_never_by_a_read(client, auth, drain):
    assert drain["plaintext"].startswith("gb_sk_")
    listed = client.get("/api/api-keys", headers=auth)
    assert listed.status_code == 200
    assert drain["plaintext"] not in listed.text
    assert all("plaintext" not in k for k in listed.json())


# ---- clause 1: claims inside the PRD; refused outside, naming the reason --------------------

def test_claim_item_outside_the_prd_is_refused_naming_the_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    """THE ONE THE SABOTAGE BREAKS. The key has no seat, so only the key's bound stops it."""
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    agent = _agent(client, drain["plaintext"], "drain-a")

    with pytest.raises(items_svc.OutOfScope) as exc:
        items_svc.claim_item(db, _stored(db, outside).id, agent)
    assert mine["id"] in str(exc.value)
    assert _stored(db, outside).claimed_by is None

    got = items_svc.claim_item(db, _stored(db, inside).id, agent)
    assert got is not None and got.claimed_by == agent


def test_claim_next_hands_out_only_the_prds_items(client, auth, drain, mine, theirs, owner_key, db):
    _item(client, owner_key, "top of the queue, other PRD", ["ops/deploy.sh"], prd_id=theirs["id"])
    _item(client, owner_key, "no PRD at all", ["src/win.rs"])
    agent = _agent(client, drain["plaintext"], "drain-a")

    empty = _ok(_mcp(client, drain["plaintext"], "claim_next", {"agent_id": agent}))
    assert empty["claimed"] is False, empty

    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    got = _ok(_mcp(client, drain["plaintext"], "claim_next", {"agent_id": agent}))
    assert got["claimed"] is True and got["item"]["id"] == inside


def test_claim_cluster_refuses_outside_and_names_the_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    agent = _agent(client, drain["plaintext"], "drain-a")

    miss = _ok(_mcp(client, drain["plaintext"], "claim_cluster", {"agent_id": agent}))
    assert miss["claimed"] is False and miss["items"] == []
    assert miss["scope"] == mine["id"]
    assert mine["id"] in miss["reason"]

    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    got = _ok(_mcp(client, drain["plaintext"], "claim_cluster", {"agent_id": agent}))
    assert got["claimed"] is True and [i["id"] for i in got["items"]] == [inside]


def test_next_cluster_takes_the_seed_and_skips_a_neighbour_from_another_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    neighbour = _item(client, owner_key, "neighbour, other PRD", ["svc/pkg/b.py"],
                      prd_id=theirs["id"])
    agent = _agent(client, drain["plaintext"], "drain-a")
    assert _ok(_mcp(client, drain["plaintext"], "next_cluster", {"agent_id": agent}))["claimed"] == 0

    seed = _item(client, owner_key, "seed", ["svc/pkg/a.py"], prd_id=mine["id"])
    got = _ok(_mcp(client, drain["plaintext"], "next_cluster", {"agent_id": agent}))
    assert [c["id"] for c in got["cluster"]] == [seed]
    assert _stored(db, neighbour).claimed_by is None


def test_claim_review_withholds_another_prds_work_naming_why(
        client, auth, drain, mine, theirs, owner_key, db):
    builder = _agent(client, owner_key, "builder")
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    _to_review(db, outside, builder)
    agent = _agent(client, drain["plaintext"], "drain-a")

    miss = _ok(_mcp(client, drain["plaintext"], "claim_review", {"agent_id": agent}))
    assert miss["claimed"] is False
    assert any(w.get("item") == outside and w.get("rule") == fleet_svc.WITHHELD_OUT_OF_SCOPE
               for w in miss["withheld"]), miss["withheld"]

    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    _to_review(db, inside, builder)
    got = _ok(_mcp(client, drain["plaintext"], "claim_review", {"agent_id": agent}))
    assert got["claimed"] is True and got["item"]["id"] == inside


def test_the_key_cannot_claim_as_an_agent_of_another_key(
        client, auth, drain, mine, theirs, owner_key, db):
    """`agent_id` is self-declared. Naming the owner's unlimited agent, or no agent at all,
    would claim with no PRD bound — so both are refused before any claim runs."""
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    borrowed = _agent(client, owner_key, "laptop session")

    for tool in ("claim_next", "claim_cluster", "next_cluster", "claim_review"):
        assert _err(_mcp(client, drain["plaintext"], tool, {"agent_id": borrowed}))
        assert _err(_mcp(client, drain["plaintext"], tool, {}))
    assert _stored(db, outside).claimed_by is None

    # Control: the owner's own key, naming its own agent, still claims it.
    got = _ok(_mcp(client, owner_key, "claim_next", {"agent_id": borrowed}))
    assert got["claimed"] is True and got["item"]["id"] == outside


# ---- clause 2: no other project, whatever project_id it passes ----------------------------

def test_a_prd_key_cannot_reach_another_project(
        client, auth, drain, other_proj, proj, mine):
    owner_other = client.post("/api/api-keys", json={"name": "other", "project_id": other_proj},
                              headers=auth).json()["plaintext"]
    _item(client, owner_other, "elsewhere's work", ["x.py"])
    agent = _agent(client, drain["plaintext"], "drain-a")

    for tool, args in (("get_backlog", {}), ("search_items", {"query": "elsewhere"}),
                       ("claim_next", {"agent_id": agent}),
                       ("create_item", {"title": "planted", "status": "next", "prd_id": mine["id"]})):
        err = _err(_mcp(client, drain["plaintext"], tool, {**args, "project_id": other_proj}))
        assert other_proj in err["message"], (tool, err)

    # Control: the same call on its own project goes through.
    assert _ok(_mcp(client, drain["plaintext"], "get_backlog", {"project_id": proj}))


# ---- clause 3: events name the key's label -------------------------------------------------

def test_calls_with_the_key_are_attributed_to_its_label(
        client, auth, drain, owner_key, proj, db, mine):
    made = _item(client, drain["plaintext"], "filed by the drain", ["a.py"], prd_id=mine["id"])
    by_owner = _item(client, owner_key, "filed by the laptop", ["b.py"])

    db.expire_all()
    rows = db.scalars(select(Event).where(Event.action == "create_item")).all()
    by_target = {}
    for e in rows:
        by_target.setdefault(e.target_id, []).append(e)
    drain_rows = [e for t, es in by_target.items() for e in es
                  if t in (made, _stored(db, made).id)]
    owner_rows = [e for t, es in by_target.items() for e in es
                  if t in (by_owner, _stored(db, by_owner).id)]
    assert drain_rows and owner_rows
    assert {(e.actor_type, e.actor_id, e.actor_label) for e in drain_rows} == {
        ("apikey", drain["id"], "drain-a @ host-1")}
    assert all(e.actor_id != drain["id"] for e in owner_rows)


# ---- clause 4: revoked means refused ------------------------------------------------------

def test_after_revoke_a_call_with_the_key_is_refused(client, auth, drain):
    assert _ok(_mcp(client, drain["plaintext"], "get_backlog"))  # control: it worked
    assert client.delete(f"/api/api-keys/{drain['id']}", headers=auth).status_code == 204

    r = _mcp_raw(client, drain["plaintext"], "get_backlog")
    assert r.status_code == 401, r.text
    r = _mcp_raw(client, drain["plaintext"], "register_agent", {"label": "after"})
    assert r.status_code == 401, r.text


# ---- clause 5: a bad prd_id mints nothing ----------------------------------------------------

def _key_count(db) -> int:
    db.expire_all()
    return len(db.scalars(select(ApiKey)).all())


def test_minting_with_a_prd_from_another_project_is_refused_and_mints_nothing(
        client, auth, proj, other_proj, db):
    foreign = _prd(client, auth, other_proj, "not this project's")
    before = _key_count(db)
    r = _mint(client, auth, prd_id=foreign["id"], project_id=proj)
    assert r.status_code == 422, r.text
    assert _key_count(db) == before


def test_minting_with_an_unknown_prd_is_refused_and_mints_nothing(client, auth, proj, db):
    before = _key_count(db)
    for bad in ("prd_does_not_exist", "NOPE-P999"):
        r = _mint(client, auth, prd_id=bad)
        assert r.status_code == 404, r.text
    assert _key_count(db) == before


def test_minting_for_a_prd_the_user_cannot_see_is_404(client, auth, other_proj, db):
    """A second user cannot mint against the first user's PRD — the key would out-rank them."""
    foreign = _prd(client, auth, other_proj, "private")
    r = client.post("/api/auth/register", json={
        "name": "Eve", "email": "eve@example.com", "handle": "eve", "password": "pw-eve-12345"})
    assert r.status_code == 201, r.text
    eve = {"Authorization": f"Bearer {r.json()['access_token']}"}
    before = _key_count(db)
    r = client.post("/api/api-keys", json={"name": "stolen", "prd_id": foreign["id"]}, headers=eve)
    assert r.status_code == 404, r.text
    assert _key_count(db) == before


def test_rotating_a_prd_key_keeps_its_prd(client, auth, drain, mine, db):
    r = client.post(f"/api/api-keys/{drain['id']}/rotate", json={}, headers=auth)
    assert r.status_code == 201, r.text
    assert db.get(ApiKey, r.json()["id"]).prd_id == mine["id"]


# ---- clause 1, the write side: no writing or re-homing another PRD's item (bounce) ---------
# Bounding claims is not enough on its own: `update_item(prd_id=<its own PRD>)` re-homed any
# item into the key's PRD and `claim_next` then handed it out. Each write tool is refused on an
# item outside the PRD with the key's named rule, next to a control on an item inside it.

PRD_RULE = "keys.prd_limited"


def _refused_by_prd(res, mine) -> dict:
    err = _err(res)
    assert err.get("rule_id") == PRD_RULE, err
    assert mine["id"] in err["message"], err
    return err


def test_a_prd_key_cannot_rehome_another_prds_item_into_its_own(
        client, auth, drain, mine, theirs, owner_key, db):
    """The reviewer's two-call route: re-home, then claim_next. It ends with nothing claimed."""
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    agent = _agent(client, drain["plaintext"], "drain-a")

    for extra in ({}, {"agent_id": agent}):
        _refused_by_prd(_mcp(client, drain["plaintext"], "update_item",
                             {"id": outside, "prd_id": mine["id"], **extra}), mine)
    assert _stored(db, outside).prd_id == theirs["id"]
    got = _ok(_mcp(client, drain["plaintext"], "claim_next", {"agent_id": agent}))
    assert got["claimed"] is False, got
    assert _stored(db, outside).claimed_by is None

    # Nor can it move its own PRD's item out to another PRD.
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    _refused_by_prd(_mcp(client, drain["plaintext"], "update_item",
                         {"id": inside, "prd_id": theirs["id"], "agent_id": agent}), mine)
    assert _stored(db, inside).prd_id == mine["id"]

    # Control: the owner's unlimited key re-homes it, and the drain then claims it.
    _ok(_mcp(client, owner_key, "update_item", {"id": outside, "prd_id": mine["id"]}))
    assert _stored(db, outside).prd_id == mine["id"]


def test_a_prd_key_cannot_update_an_item_outside_its_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    agent = _agent(client, drain["plaintext"], "drain-a")

    for extra in ({}, {"agent_id": agent}):
        _refused_by_prd(_mcp(client, drain["plaintext"], "update_item",
                             {"id": outside, "title": "renamed", "status": "in_progress",
                              **extra}), mine)
    row = _stored(db, outside)
    assert row.title == "other PRD's work" and row.status == "next"

    # Control: its own PRD's item, including naming its own PRD, goes through.
    _ok(_mcp(client, drain["plaintext"], "update_item",
             {"id": inside, "title": "renamed", "prd_id": mine["id"], "agent_id": agent}))
    assert _stored(db, inside).title == "renamed"


def test_a_prd_key_cannot_heartbeat_an_item_outside_its_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    holder = _agent(client, owner_key, "laptop session")
    _ok(_mcp(client, owner_key, "claim_next", {"agent_id": holder}))
    agent = _agent(client, drain["plaintext"], "drain-a")

    _refused_by_prd(_mcp(client, drain["plaintext"], "heartbeat",
                         {"agent_id": agent, "id": outside}), mine)
    assert _stored(db, outside).claimed_by == holder

    # Control: heartbeating its own claimed item, and a bare presence heartbeat, go through.
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    assert _ok(_mcp(client, drain["plaintext"], "claim_next", {"agent_id": agent}))["claimed"]
    _ok(_mcp(client, drain["plaintext"], "heartbeat", {"agent_id": agent, "id": inside}))
    _ok(_mcp(client, drain["plaintext"], "heartbeat", {"agent_id": agent}))


def test_a_prd_key_cannot_release_an_item_outside_its_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    holder = _agent(client, owner_key, "laptop session")
    _ok(_mcp(client, owner_key, "claim_next", {"agent_id": holder}))
    agent = _agent(client, drain["plaintext"], "drain-a")

    for extra in ({}, {"agent_id": agent}):
        _refused_by_prd(_mcp(client, drain["plaintext"], "release_item",
                             {"id": outside, **extra}), mine)
    assert _stored(db, outside).claimed_by == holder

    # Control: it releases its own PRD's item that it holds.
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    assert _ok(_mcp(client, drain["plaintext"], "claim_next", {"agent_id": agent}))["claimed"]
    _ok(_mcp(client, drain["plaintext"], "release_item", {"id": inside, "agent_id": agent}))
    assert _stored(db, inside).claimed_by is None


def test_a_prd_key_cannot_link_an_item_outside_its_prd_at_either_end(
        client, auth, drain, mine, theirs, owner_key, db):
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    other_inside = _item(client, owner_key, "more drain work", ["svc/b.py"], prd_id=mine["id"])
    agent = _agent(client, drain["plaintext"], "drain-a")

    for a, b in ((inside, outside), (outside, inside)):
        _refused_by_prd(_mcp(client, drain["plaintext"], "link_items",
                             {"a": a, "b": b, "type": "dependency", "agent_id": agent}), mine)

    # Control: both ends inside its PRD.
    got = _ok(_mcp(client, drain["plaintext"], "link_items",
                   {"a": inside, "b": other_inside, "type": "dependency", "agent_id": agent}))
    assert got["type"] == "dependency"


def test_a_prd_key_cannot_bounce_an_item_outside_its_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    builder = _agent(client, owner_key, "builder")
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    _to_review(db, outside, builder)
    agent = _agent(client, drain["plaintext"], "drain-a")

    for extra in ({}, {"agent_id": agent}):
        _refused_by_prd(_mcp(client, drain["plaintext"], "bounce",
                             {"id": outside, "reason": "planted", **extra}), mine)
    assert _stored(db, outside).status == "review"

    # Control: it bounces its own PRD's review item that it claimed for review.
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    _to_review(db, inside, builder)
    assert _ok(_mcp(client, drain["plaintext"], "claim_review", {"agent_id": agent}))["claimed"]
    _ok(_mcp(client, drain["plaintext"], "bounce",
             {"id": inside, "reason": "missing a test", "agent_id": agent}))
    assert _stored(db, inside).status != "review"


def test_a_prd_key_cannot_sign_off_an_item_outside_its_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    builder = _agent(client, owner_key, "builder")
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    _to_review(db, outside, builder)
    agent = _agent(client, drain["plaintext"], "drain-a")

    for extra in ({}, {"agent_id": agent}):
        _refused_by_prd(_mcp(client, drain["plaintext"], "sign_off",
                             {"id": outside, **extra}), mine)
    row = _stored(db, outside)
    assert row.status == "review" and row.reviewed_by is None

    # Control: on its own PRD's review item the PRD bound does not refuse; whatever the
    # completion gate then says, it is not this rule.
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    _to_review(db, inside, builder)
    assert _ok(_mcp(client, drain["plaintext"], "claim_review", {"agent_id": agent}))["claimed"]
    res = _mcp(client, drain["plaintext"], "sign_off", {"id": inside, "agent_id": agent})
    if res.get("isError"):
        assert res["structuredContent"]["error"].get("rule_id") != PRD_RULE, res


def test_an_unlimited_key_still_writes_any_prds_item(client, auth, theirs, mine, owner_key, db):
    """The bound is the key's, not the tools': an ordinary key is untouched by it."""
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    agent = _agent(client, owner_key, "laptop session")
    _ok(_mcp(client, owner_key, "update_item", {"id": outside, "title": "renamed"}))
    _ok(_mcp(client, owner_key, "link_items",
             {"a": inside, "b": outside, "type": "dependency", "agent_id": agent}))
    assert _ok(_mcp(client, owner_key, "claim_next", {"agent_id": agent}))["claimed"]


# ---- GRPH-1985: the writes that still reached outside the PRD ------------------------------
# Each is refused with the same named refusal update_item gives, beside a control inside the
# PRD that goes through. Sabotage: drop the tool's entry from `PRD_KEY_ITEM_WRITES` or
# `PRD_KEY_PRD_ARGS` in app/security/authz.py and its refusal test fails.

def _drain_planner(client, auth, drain, proj) -> str:
    """A planner registered on the drain's own key, so delegate and mint reach the PRD bound."""
    code = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["planner"], "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    return _ok(_mcp(client, drain["plaintext"], "register_agent",
                    {"label": "drain planner", "enrolment_code": code}))["agent_id"]


def test_a_prd_key_cannot_create_an_item_in_another_prd_or_none(
        client, auth, drain, mine, theirs, owner_key, db):
    key = drain["plaintext"]
    before = db.scalar(select(func.count()).select_from(Item))
    _refused_by_prd(_mcp(client, key, "create_item",
                         {"title": "planted", "prd_id": theirs["id"]}), mine)
    _refused_by_prd(_mcp(client, key, "create_item", {"title": "planted"}), mine)
    # Nor may it hang a new item off another PRD's item.
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    _refused_by_prd(_mcp(client, key, "create_item",
                         {"title": "planted", "prd_id": mine["id"], "kind": "fix",
                          "blocks": outside}), mine)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Item)) == before + 1  # only `outside`

    # Control: an item in its own PRD.
    made = _item(client, key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    assert _stored(db, made).prd_id == mine["id"]


def test_a_prd_key_cannot_unlink_an_item_outside_its_prd_at_either_end(
        client, auth, drain, mine, theirs, owner_key, db):
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    other_inside = _item(client, owner_key, "more drain work", ["svc/b.py"], prd_id=mine["id"])
    _ok(_mcp(client, owner_key, "link_items", {"a": inside, "b": outside, "type": "dependency"}))
    _ok(_mcp(client, owner_key, "link_items",
             {"a": inside, "b": other_inside, "type": "dependency"}))

    for a, b in ((inside, outside), (outside, inside)):
        _refused_by_prd(_mcp(client, drain["plaintext"], "unlink_items",
                             {"a": a, "b": b, "type": "dependency"}), mine)
    # The cross-PRD link is still there for its owner to remove.
    assert _ok(_mcp(client, owner_key, "unlink_items",
                    {"a": inside, "b": outside, "type": "dependency"}))["removed"]

    # Control: both ends inside its PRD.
    assert _ok(_mcp(client, drain["plaintext"], "unlink_items",
                    {"a": inside, "b": other_inside, "type": "dependency"}))["removed"]


def test_a_prd_key_cannot_link_code_to_an_item_outside_its_prd(
        client, auth, drain, mine, theirs, owner_key, db):
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])

    _refused_by_prd(_mcp(client, drain["plaintext"], "link_code",
                         {"ref_id": outside, "path": "ops/deploy.sh"}), mine)
    _ok(_mcp(client, drain["plaintext"], "link_code", {"ref_id": inside, "path": "svc/a.py"}))

    # unlink_code: the owner links the outside item; the drain cannot remove it.
    _ok(_mcp(client, owner_key, "link_code", {"ref_id": outside, "path": "ops/deploy.sh"}))
    _refused_by_prd(_mcp(client, drain["plaintext"], "unlink_code",
                         {"ref_id": outside, "path": "ops/deploy.sh"}), mine)
    assert _ok(_mcp(client, owner_key, "unlink_code",
                    {"ref_id": outside, "path": "ops/deploy.sh"}))["removed"]

    # Control: unlinking its own PRD's item goes through.
    assert _ok(_mcp(client, drain["plaintext"], "unlink_code",
                    {"ref_id": inside, "path": "svc/a.py"}))["removed"]


def test_a_prd_key_cannot_link_code_to_an_item_outside_its_prd_writes_nothing(
        client, auth, drain, mine, theirs, owner_key, db):
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    _refused_by_prd(_mcp(client, drain["plaintext"], "link_code",
                         {"ref_id": outside, "path": "ops/deploy.sh"}), mine)
    # Nothing to unlink: the refused call stored no edge.
    assert not _ok(_mcp(client, owner_key, "unlink_code",
                        {"ref_id": outside, "path": "ops/deploy.sh"}))["removed"]


def test_a_prd_key_cannot_delegate_outside_its_prd(
        client, auth, drain, mine, theirs, owner_key, proj, db):
    from app.models import Enrolment

    planner = _drain_planner(client, auth, drain, proj)
    key = drain["plaintext"]
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    inside = _item(client, owner_key, "the drain's work", ["svc/a.py"], prd_id=mine["id"])
    base = {"lane": "backend", "tier": "cheap", "agent_id": planner}
    seats = db.scalar(select(func.count()).select_from(Enrolment))

    _refused_by_prd(_mcp(client, key, "delegate", {"id": outside, **base}), mine)
    _refused_by_prd(_mcp(client, key, "delegate",
                         {"id": inside, "seat": True, "scope": theirs["id"], **base}), mine)
    _refused_by_prd(_mcp(client, key, "delegate", {"id": inside, "seat": True, **base}), mine)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Enrolment)) == seats

    # Control: its own item, a seat scoped to its own PRD.
    got = _ok(_mcp(client, key, "delegate",
                   {"id": inside, "seat": True, "scope": mine["id"], **base}))
    assert got["enrolment_code"]


def test_a_prd_key_cannot_mint_a_seat_outside_its_prd(
        client, auth, drain, mine, theirs, owner_key, proj, db):
    from app.models import Enrolment

    planner = _drain_planner(client, auth, drain, proj)
    key = drain["plaintext"]
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    base = {"agent_id": planner, "role": "worker"}
    seats = db.scalar(select(func.count()).select_from(Enrolment))

    _refused_by_prd(_mcp(client, key, "mint_enrolment", {**base, "scope": theirs["id"]}), mine)
    _refused_by_prd(_mcp(client, key, "mint_enrolment", base), mine)
    _refused_by_prd(_mcp(client, key, "mint_enrolment",
                         {**base, "scope": mine["id"], "bound_only": True, "item": outside}),
                    mine)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Enrolment)) == seats

    # Control: a seat scoped to its own PRD.
    assert _ok(_mcp(client, key, "mint_enrolment", {**base, "scope": mine["id"]}))["enrolment_code"]


def test_an_unlimited_key_still_creates_links_and_mints_anywhere(
        client, auth, theirs, mine, owner_key, proj, db):
    outside = _item(client, owner_key, "other PRD's work", ["ops/deploy.sh"], prd_id=theirs["id"])
    _item(client, owner_key, "no PRD at all", ["x.py"])
    _ok(_mcp(client, owner_key, "link_code", {"ref_id": outside, "path": "ops/deploy.sh"}))
    assert _ok(_mcp(client, owner_key, "unlink_code",
                    {"ref_id": outside, "path": "ops/deploy.sh"}))["removed"]
    code = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["planner"], "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    planner = _ok(_mcp(client, owner_key, "register_agent",
                       {"label": "p", "enrolment_code": code}))["agent_id"]
    assert _ok(_mcp(client, owner_key, "mint_enrolment",
                    {"agent_id": planner, "role": "worker"}))["enrolment_code"]
    assert _ok(_mcp(client, owner_key, "delegate",
                    {"id": outside, "lane": "backend", "tier": "cheap", "agent_id": planner,
                     "seat": True}))["enrolment_code"]
