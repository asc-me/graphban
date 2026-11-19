"""GRPH-1713: a project's preference orders entitlements WITHIN every ladder rung.

Subscriptions, API keys and local models can each be ordered among themselves (an explicit order
under custom hierarchy, and a tie-break of the rung's own). The ladder order — subscription, then
API key, then local — stays fixed: no rung's preference lifts an entitlement past it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from test_entitlements import _ok, hosted, org  # noqa: F401  (fixtures)
from test_routing import _P, _claim, _decisions, _ent, _item, _report, _runner, world  # noqa: F401
from test_routing_tie_break_hold import _policy, _rank, _runner_on, rung  # noqa: F401

NOW = datetime.now(timezone.utc)


def _custom(**pref):
    _policy(routing_mode="custom_hierarchy", rung_preference=pref)


def _ids(ranked):
    return [e["id"] for e in ranked]


def test_with_no_preference_the_pick_is_exactly_todays(client, rung):
    before = _ids(_rank())
    _policy(routing_mode="custom_hierarchy")
    assert _ids(_rank()) == before
    _policy(rung_preference={"api_key": {"order": [rung["cheap"], rung["roomy"]]}})
    # An order outside custom hierarchy is not stored, as `subscription_order` is not.
    assert _ids(_rank()) == before
    assert before[0] == rung["soon"]


def test_an_api_key_order_reorders_two_api_keys(client, rung):
    _custom(api_key={"order": [rung["roomy"], rung["cheap"]]})
    ranked = _rank()
    # Ranked ones in policy order; the unranked one after every ranked one.
    assert _ids(ranked) == [rung["roomy"], rung["cheap"], rung["soon"]]


def test_a_local_order_reorders_two_local_models(client, world):
    first = _ent(client, world, "ollama", kind="local")
    second = _ent(client, world, "lmstudio", kind="local")
    a = _runner(client, world, [first, second])
    for e in (first, second):
        _report(client, world, e, a, "registered")
    assert _ids([e for e in _rank() if e["rung"] == "local"]) == [first, second]
    _custom(local={"order": [second, first]})
    assert _ids([e for e in _rank() if e["rung"] == "local"]) == [second, first]


def test_no_api_key_order_lifts_an_api_key_above_a_subscription_with_headroom(client, world,
                                                                               rung):
    """The sabotage target: let any rung's order be read across rungs and this goes red.

    The subscription with headroom is SECOND in its own rung's order (preference_rank 1) while
    the api key is FIRST in its rung's (preference_rank 0), so only the ladder keeps the
    subscription above the api key: a sort that reads preference ahead of the rung lifts it."""
    first = _ent(client, world, "google", kind="subscription")
    sub = _ent(client, world, "openai", kind="subscription")
    _report(client, world, first, _runner(client, world, [first], vendor="google"), "registered")
    _report(client, world, sub, _runner(client, world, [sub], vendor="openai"), "registered")
    _policy(routing_mode="custom_hierarchy", subscription_order=[first, sub],
            rung_preference={"api_key": {"order": [rung["cheap"]], "tie_break": "cheapest"}})
    ranked = _rank()
    assert [e["rung"] for e in ranked[:2]] == ["subscription", "subscription"]
    assert _ids(ranked[:2]) == [first, sub]
    assert ranked[1]["preference_rank"] == 1
    assert ranked[2]["id"] == rung["cheap"] and ranked[2]["preference_rank"] == 0


def test_a_rungs_own_tie_break_applies_only_within_that_rung(client, rung):
    _policy(rung_preference={"api_key": {"tie_break": "most_headroom"},
                             "local": {"tie_break": "cheapest"}})
    ranked = _rank()
    assert ranked[0]["id"] == rung["roomy"]
    assert ranked[0]["tie_break"] == "most_headroom"


def test_the_reason_names_the_api_key_order_when_it_decided(client, rung):
    _custom(api_key={"order": [rung["cheap"]]})
    item = _item()
    assert _claim(rung["claimant"], item) == item
    (rec,) = [d for d in _decisions(item) if d["kind"] == "claim"]
    assert rec["entitlement_id"] == rung["cheap"]
    assert (f"chosen over {rung['soon']}, {rung['roomy']} by api key policy order"
            in rec["reason"])
    assert "soonest reset" not in rec["reason"]


def test_replay_reranks_under_a_proposed_rung_order(client, rung):
    from app.db import SessionLocal
    from app.services import routing

    item = _item()
    assert _claim(rung["claimant"], item) == item
    db = SessionLocal()
    try:
        out = routing.replay(db, _P["id"], routing.replay_proposal(
            routing_mode="custom_hierarchy", rung_order={"api_key": [rung["roomy"]]}))
        tied = routing.replay(db, _P["id"], routing.replay_proposal(
            rung_tie_break={"api_key": "cheapest"}))
    finally:
        db.close()
    assert out["moved"] == 1
    assert out["examples"][0]["to"]["entitlement_id"] == rung["roomy"]
    assert tied["moved"] == 1
    assert tied["examples"][0]["to"]["entitlement_id"] == rung["cheap"]


def test_the_snapshot_records_the_rung_preference_in_force(client, rung):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import RoutingDecision

    _custom(api_key={"order": [rung["cheap"]], "tie_break": "cheapest"})
    item = _item()
    assert _claim(rung["claimant"], item) == item
    db = SessionLocal()
    try:
        (row,) = db.scalars(select(RoutingDecision).where(RoutingDecision.item_id == item)).all()
        policy = row.candidates["policy"]
    finally:
        db.close()
    assert policy["rung_order"] == {"api_key": [rung["cheap"]]}
    assert policy["rung_tie_break"] == {"api_key": "cheapest"}


@pytest.mark.parametrize("bad", [
    "x", {"gold": {}}, {"api_key": []}, {"api_key": {"rank": 1}},
    {"api_key": {"order": "a"}}, {"api_key": {"order": ["a", "a"]}},
    {"local": {"tie_break": "fastest"}}, {"subscription": {"order": ["a"]}}])
def test_a_rung_preference_is_validated(bad):
    from app.services import fleet_profiles

    with pytest.raises(fleet_profiles.ProfileInvalid):
        fleet_profiles.normalise_policy({"routing_mode": "custom_hierarchy",
                                         "rung_preference": bad})


def test_the_policy_write_round_trips_the_rung_preference(client, world):
    body = {"project_id": world["project"]["id"], "routing_mode": "custom_hierarchy",
            "subscription_order": ["s1"],
            "rung_preference": {"api_key": {"order": ["k2", "k1"], "tie_break": "cheapest"},
                                "local": {"order": ["l1"]}}}
    r = client.put("/api/fleet/policy", json=body, headers=world["owner"])
    assert r.status_code == 200, r.text
    got = client.get("/api/fleet/policy", params={"project_id": world["project"]["id"]},
                     headers=world["owner"]).json()["policy"]
    assert got["rung_preference"] == body["rung_preference"]
    assert got["subscription_order"] == ["s1"]
    bad = client.put("/api/fleet/policy", headers=world["owner"],
                     json={**body, "rung_preference": {"gold": {}}})
    assert bad.status_code == 422


def test_the_replay_route_takes_rung_orders_and_tie_breaks(client, world, rung):
    item = _item()
    assert _claim(rung["claimant"], item) == item
    r = client.get("/api/harness/capacity/replay", headers=world["owner"],
                   params={"project_id": _P["id"], "routing_mode": "custom_hierarchy",
                           "api_key_order": rung["roomy"]})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 1
    bad = client.get("/api/harness/capacity/replay", headers=world["owner"],
                     params={"project_id": _P["id"], "api_key_tie_break": "fastest"})
    assert bad.status_code == 422
