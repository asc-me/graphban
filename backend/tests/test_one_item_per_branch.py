"""GRPH-948: a branch and a PR carry exactly one item.

Reported from SA-P21: one child built four items on one branch and the PR carried all four —
19 files, ~2,500 lines, merged before review, four fix PRs after. A cluster claim is a
scheduling unit, not a delivery unit. The server half: a bound seat cannot claim a second item
while its own is open, and `claim_cluster`'s `max_items` has a project ceiling per lane.
"""
import pytest

from app.models import Item, Project
from app.services import fleet
from app.services import fleet_profiles as profiles_svc
from app.services import items as items_svc


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


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
    return client.post("/api/projects", json={"name": "OneItemPerBranch"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "one-item", "project_id": proj,
                             "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


def _item(client, key, title, areas):
    return _ok(client, key, "create_item",
               {"title": title, "status": "next", "touchpoints": areas})


def _bound_worker(client, key, item_id):
    planner = _ok(client, key, "register_agent",
                  {"branch": "gb/plan", "label": "planner", "capabilities": {"instance": "planner"}})
    seat = _ok(client, key, "delegate",
               {"id": item_id, "lane": "backend", "tier": "cheap",
                "agent_id": planner["agent_id"], "seat": True, "wave": "w"})
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/child", "label": "child", "enrolment_code": seat["enrolment_code"],
                  "worktree": "/tmp/child"})
    assert worker["assigned"]["state"] == "claimed", worker
    return worker["agent_id"]


# ---- a bound seat takes one item at a time --------------------------------------------------

def test_a_bound_seat_cannot_claim_a_second_item_while_its_own_is_open(client, key, db):
    bound = _item(client, key, "bound", ["backend/app/one.py"])
    other = _item(client, key, "other", ["backend/app/two.py"])
    worker = _bound_worker(client, key, bound["id"])

    with pytest.raises(items_svc.BoundSeatBusy) as refused:
        items_svc.claim_item(db, other["id"], worker)
    assert bound["id"] in str(refused.value) and "review" in str(refused.value)
    assert db.get(Item, other["id"]).claimed_by is None, "refused, and nothing was taken"


def test_the_bound_seat_is_refused_even_after_letting_go_of_its_item(client, key, db):
    """Releasing is not finishing. A seat that drops its item and takes another has still
    carried two items through one worktree."""
    bound = _item(client, key, "bound", ["backend/app/one.py"])
    other = _item(client, key, "other", ["backend/app/two.py"])
    worker = _bound_worker(client, key, bound["id"])
    items_svc.release_item(db, bound["id"], worker)

    with pytest.raises(items_svc.BoundSeatBusy):
        items_svc.claim_item(db, other["id"], worker)
    got = fleet.claim_cluster(db, agent_id=worker, project_id=db.get(Item, other["id"]).project_id)
    assert got["claimed"] is False and bound["id"] in got["reason"]


def test_once_the_bound_item_is_in_review_the_seat_may_claim_again(client, key, db):
    bound = _item(client, key, "bound", ["backend/app/one.py"])
    other = _item(client, key, "other", ["backend/app/two.py"])
    worker = _bound_worker(client, key, bound["id"])
    row = db.get(Item, bound["id"])
    row.status = "review"
    db.commit()

    assert items_svc.claim_item(db, other["id"], worker) is not None


def test_an_unbound_worker_is_not_affected(client, key, db):
    first = _item(client, key, "a", ["backend/app/one.py"])
    second = _item(client, key, "b", ["backend/app/two.py"])
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/w", "label": "w", "role_hint": "worker",
                  "capabilities": {"instance": "w"}})["agent_id"]

    assert items_svc.claim_item(db, first["id"], worker) is not None
    assert items_svc.claim_item(db, second["id"], worker) is not None


# ---- claim_cluster's max_items has a lane ceiling ---------------------------------------------

def _worker(client, key, label):
    return _ok(client, key, "register_agent",
               {"branch": "gb/w", "label": label, "role_hint": "worker",
                "capabilities": {"instance": label}})["agent_id"]


def test_a_web_cluster_hands_out_one_item_whatever_the_caller_asks(client, key, db, proj):
    for n in range(3):
        _item(client, key, f"web {n}", ["web/src/features/board/Board.tsx"])
    worker = _worker(client, key, "w1")

    got = _ok(client, key, "claim_cluster", {"agent_id": worker, "max_items": 3})

    assert got["claimed"] and len(got["items"]) == 1, got


def test_a_backend_cluster_keeps_the_callers_max_items(client, key, db, proj):
    """The control: without it, a ceiling that bound every lane would pass the web test."""
    for n in range(3):
        _item(client, key, f"api {n}", ["backend/app/services/spanner.py"])
    worker = _worker(client, key, "w1")

    got = _ok(client, key, "claim_cluster", {"agent_id": worker, "max_items": 3})

    assert got["claimed"] and len(got["items"]) == 3, got


def test_the_project_policy_sets_the_ceiling_and_the_caller_cannot_raise_it(client, key, db, proj):
    for n in range(4):
        _item(client, key, f"api {n}", ["backend/app/services/spanner.py"])
    profiles_svc.set_policy(db, db.get(Project, proj), {"cluster_ceiling": {"backend": 2}})
    worker = _worker(client, key, "w1")

    got = _ok(client, key, "claim_cluster", {"agent_id": worker, "max_items": 4})

    assert got["claimed"] and len(got["items"]) == 2, got


def test_a_mixed_cluster_is_held_to_the_web_ceiling(client, key, db, proj):
    _item(client, key, "both", ["web/src/lib/api.ts", "backend/app/routers/x.py"])
    _item(client, key, "both 2", ["web/src/lib/api.ts", "backend/app/routers/x.py"])
    worker = _worker(client, key, "w1")

    got = _ok(client, key, "claim_cluster", {"agent_id": worker, "max_items": 3})

    assert got["claimed"] and len(got["items"]) == 1, got


def test_the_ceiling_policy_is_validated():
    assert profiles_svc.normalise_policy({"cluster_ceiling": {"frontend": 2}}) == {
        "local_only": False, "reviewer_cross_vendor": False, "allowed_harnesses": [],
        "cluster_ceiling": {"frontend": 2}}
    for bad in ({"web": 1}, {"frontend": 0}, {"frontend": True}, [1]):
        with pytest.raises(profiles_svc.ProfileInvalid):
            profiles_svc.normalise_policy({"cluster_ceiling": bad})
