"""GRPH-1505: a project chooses the router's tie-break, and whether to hold an item until a reset
instead of routing it down a rung.

The tie-break is only the LAST sort key: on one rung, soonest reset, most headroom and cheapest
each pick a different entitlement, and none of them lifts a lower rung over a higher one. Hold
until reset refuses to route down a rung and names the earliest stated reset, or says none is.
"""
from datetime import datetime, timedelta, timezone

import pytest

from test_entitlements import _ok, hosted, org  # noqa: F401  (fixtures)
from test_routing import _P, _claim, _decisions, _ent, _item, _report, _runner, world  # noqa: F401

NOW = datetime.now(timezone.utc)


def _runner_on(client, org, ents, vendor, model):
    return _ok(client, org["key"], "register_agent",
               {"label": f"r-{vendor}-{model}", "entitlements": ents,
                "capabilities": {"vendor": vendor, "model": model}})["agent_id"]


def _policy(**keys):
    from app.db import SessionLocal
    from app.models import Project
    from app.services import fleet_profiles

    db = SessionLocal()
    try:
        project = db.get(Project, _P["id"])
        project.fleet_policy = fleet_profiles.normalise_policy({
            "local_only": False, "reviewer_cross_vendor": False, "allowed_harnesses": [], **keys})
        db.commit()
    finally:
        db.close()


def _rank():
    from app.db import SessionLocal
    from app.models import Project
    from app.services import routing

    db = SessionLocal()
    try:
        return routing.rank(db, db.get(Project, _P["id"]).org_id, project_id=_P["id"])
    finally:
        db.close()


def _gate(item_id, agent):
    from app.db import SessionLocal
    from app.services import items, routing

    db = SessionLocal()
    try:
        return routing.gate(db, items.get_item(db, item_id), agent)
    finally:
        db.close()


@pytest.fixture()
def rung(client, world):
    """Three api keys on one rung, each best under a different tie-break, and a subscription
    above them that states nothing, so it loses every tie-break and still ranks first."""
    soon = _ent(client, world, "anthropic", reset=NOW + timedelta(hours=1), budget=1000)
    roomy = _ent(client, world, "openai", reset=NOW + timedelta(days=3), budget=100_000)
    cheap = _ent(client, world, "deepseek", reset=NOW + timedelta(days=2))
    # The claimant declares all three, so its own model is one of the prices each is read at.
    a = _runner_on(client, world, [soon, roomy, cheap], "deepseek", "deepseek-chat")
    _runner_on(client, world, [soon], "anthropic", "claude-opus-4")
    _runner_on(client, world, [roomy], "openai", "gpt-5")
    for e in (soon, roomy, cheap):
        _report(client, world, e, a, "registered")
    _report(client, world, soon, a, "spend", tokens=900)  # 100 left of 1000
    return {"soon": soon, "roomy": roomy, "cheap": cheap, "claimant": a}


@pytest.mark.parametrize("tie_break,winner", [
    (None, "soon"), ("soonest_reset", "soon"), ("most_headroom", "roomy"), ("cheapest", "cheap")])
def test_each_tie_break_picks_a_different_entitlement_on_the_same_rung(client, rung, tie_break,
                                                                     winner):
    if tie_break:
        _policy(tie_break=tie_break)
    ranked = _rank()
    assert ranked[0]["id"] == rung[winner]
    assert {e["rung"] for e in ranked} == {"api_key"}


@pytest.mark.parametrize("tie_break", ["soonest_reset", "most_headroom", "cheapest"])
def test_no_tie_break_lifts_a_lower_rung_over_a_higher_one(client, world, rung, tie_break):
    sub = _ent(client, world, "google", kind="subscription")  # no reset, no budget: loses ties
    _report(client, world, sub, _runner(client, world, [sub], vendor="google"), "registered")
    _policy(tie_break=tie_break)
    ranked = _rank()
    assert ranked[0]["id"] == sub and ranked[0]["rung"] == "subscription"
    assert ranked[1]["rung"] == "api_key"


@pytest.mark.parametrize("tie_break,winner,phrase", [
    ("most_headroom", "roomy", "by most headroom"), ("cheapest", "cheap", "by cheapest")])
def test_the_reason_names_the_tie_break_that_decided(client, rung, tie_break, winner, phrase):
    _policy(tie_break=tie_break)
    item = _item()
    assert _claim(rung["claimant"], item) == item
    (rec,) = [d for d in _decisions(item) if d["kind"] == "claim"]
    assert rec["entitlement_id"] == rung[winner]
    assert phrase in rec["reason"]
    assert "soonest reset" not in rec["reason"]


def test_the_default_reason_still_credits_soonest_reset(client, rung):
    item = _item()
    assert _claim(rung["claimant"], item) == item
    (rec,) = [d for d in _decisions(item) if d["kind"] == "claim"]
    assert rec["entitlement_id"] == rung["soon"]
    assert f"chosen over {rung['cheap']}, {rung['roomy']} by soonest reset" in rec["reason"]


def test_an_entitlement_stating_nothing_is_tied_not_beaten(client, rung):
    """Most headroom: `cheap` states no budget, so it neither beats nor loses on headroom."""
    from app.services import routing

    _policy(tie_break="most_headroom")
    ranked = _rank()
    assert [e["id"] for e in ranked] == [rung["roomy"], rung["soon"], rung["cheap"]]
    why = routing._why(ranked[2], [e for e in ranked if e["id"] == rung["cheap"]]
                       + [{**ranked[0], "headroom_tokens": None}])
    assert "no stated headroom separates it from" in why and "creation order decides" in why


# ---- hold until reset ---------------------------------------------------------------------

@pytest.fixture()
def spent_top(client, world):
    """The subscription rung is spent; an api key the claimant holds has headroom below it."""
    early = _ent(client, world, "anthropic", kind="subscription", reset=NOW + timedelta(hours=2))
    later = _ent(client, world, "openai", kind="subscription", reset=NOW + timedelta(hours=5))
    key = _ent(client, world, "mistral")
    a = _runner(client, world, [early, later, key])
    _report(client, world, key, a, "registered")
    for e in (later, early):
        _report(client, world, e, a, "limit_hit", vendor_text="limit")
    _runner(client, world, [_ent(client, world, "google")], vendor="google")  # reviewer vendor
    return {"early": early, "later": later, "key": key, "claimant": a}


def test_fall_through_is_the_default_and_routes_down_a_rung(client, spent_top):
    item = _item()
    assert _claim(spent_top["claimant"], item) == item
    (rec,) = [d for d in _decisions(item) if d["kind"] == "claim"]
    assert rec["entitlement_id"] == spent_top["key"]


def test_hold_does_not_route_down_a_rung_and_names_the_earliest_stated_reset(client, spent_top):
    _policy(when_exhausted="hold_until_reset")
    item = _item()
    verdict = _gate(item, spent_top["claimant"])
    assert verdict["allowed"] is False and verdict["entitlement_id"] is None
    assert spent_top["early"] in verdict["reason"] and spent_top["later"] not in verdict["reason"]
    assert (NOW + timedelta(hours=2)).isoformat()[:16] in verdict["reason"]
    assert "not routed down a rung" in verdict["reason"]
    assert _claim(spent_top["claimant"], item) is None
    assert [d for d in _decisions(item) if d["kind"] == "claim"] == []


def test_hold_says_so_when_no_reset_is_stated(client, world):
    sub = _ent(client, world, "anthropic", kind="subscription")
    key = _ent(client, world, "mistral")
    a = _runner(client, world, [sub, key])
    _report(client, world, key, a, "registered")
    _report(client, world, sub, a, "limit_hit", vendor_text="limit")
    _runner(client, world, [_ent(client, world, "google")], vendor="google")
    _policy(when_exhausted="hold_until_reset")
    item = _item()
    verdict = _gate(item, a)
    assert verdict["allowed"] is False
    assert f"no reset is stated for {sub}" in verdict["reason"]


def test_hold_still_builds_on_the_top_rung_when_it_has_headroom(client, world):
    sub = _ent(client, world, "anthropic", kind="subscription")
    a = _runner(client, world, [sub])
    _report(client, world, sub, a, "registered")
    _runner(client, world, [_ent(client, world, "google")], vendor="google")
    _policy(when_exhausted="hold_until_reset")
    item = _item()
    assert _claim(a, item) == item


# ---- policy -------------------------------------------------------------------------------

def test_both_controls_round_trip_through_the_policy_write(client, auth):
    proj = client.post("/api/projects", json={"name": "Ties"}, headers=auth).json()["id"]
    body = {"project_id": proj, "tie_break": "cheapest", "when_exhausted": "hold_until_reset"}
    r = client.put("/api/fleet/policy", json=body, headers=auth)
    assert r.status_code == 200, r.text
    got = client.get(f"/api/fleet/policy?project_id={proj}", headers=auth).json()["policy"]
    assert got["tie_break"] == "cheapest" and got["when_exhausted"] == "hold_until_reset"


def test_defaults_are_today_and_are_not_stored():
    from app.services import fleet_profiles

    assert fleet_profiles.normalise_policy({"tie_break": "soonest_reset",
                                            "when_exhausted": "fall_through"}) is None
    with pytest.raises(fleet_profiles.ProfileInvalid, match="tie_break must be one of"):
        fleet_profiles.normalise_policy({"tie_break": "random"})
    with pytest.raises(fleet_profiles.ProfileInvalid, match="when_exhausted must be one of"):
        fleet_profiles.normalise_policy({"when_exhausted": "wait"})
