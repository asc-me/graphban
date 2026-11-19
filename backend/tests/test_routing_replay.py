"""GRPH-1712: each routing decision records the ranked candidates it was made from, and a replay
re-ranks a window of them under a proposed policy.

Replay reads the candidates as they were THEN: it never simulates today's headroom against old
items. A decision recorded before candidates were stored is "not replayable", never "would not
have moved".
"""
from datetime import datetime, timedelta, timezone

import pytest

from test_entitlements import _ok, hosted, org  # noqa: F401  (fixtures)
from test_routing import _P, _claim, _ent, _item, _report, _runner, world  # noqa: F401
from test_routing_tie_break_hold import _policy, _runner_on, rung, spent_top  # noqa: F401

NOW = datetime.now(timezone.utc)


def _replay(**proposal):
    from app.db import SessionLocal
    from app.services import routing

    db = SessionLocal()
    try:
        return routing.replay(db, _P["id"], routing.replay_proposal(**proposal))
    finally:
        db.close()


def _rows(item_id):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import RoutingDecision

    db = SessionLocal()
    try:
        return [(r.kind, r.entitlement_id, r.candidates) for r in db.scalars(
            select(RoutingDecision).where(RoutingDecision.item_id == item_id)
            .order_by(RoutingDecision.id)).all()]
    finally:
        db.close()


def test_a_claim_records_its_ranked_candidates_with_every_tie_break_input(client, rung):
    item = _item()
    assert _claim(rung["claimant"], item) == item
    ((kind, chosen, snap),) = _rows(item)
    assert kind == "claim" and chosen == rung["soon"]
    assert snap["policy"] == {"ladder": ["subscription", "api_key", "local"],
                              "tie_break": "soonest_reset", "when_exhausted": "fall_through",
                              "subscription_order": None}
    assert set(snap["declared"]) == {rung["soon"], rung["roomy"], rung["cheap"]}
    ents = snap["entitlements"]
    # Stored in creation order, each with its creation index and its rank under the policy in force.
    assert [e["id"] for e in ents] == [rung["soon"], rung["roomy"], rung["cheap"]]
    assert [e["created"] for e in ents] == [0, 1, 2]
    assert {e["id"]: e["rank"] for e in ents} == {rung["soon"]: 0, rung["cheap"]: 1,
                                                  rung["roomy"]: 2}
    soon = ents[0]
    assert soon["kind"] == "api_key" and soon["rung"] == "api_key"
    assert soon["state"] == "available" and soon["rank"] == 0
    assert datetime.fromisoformat(soon["next_reset_at"]) > NOW
    # Every tie-break's input is stored, not only the one in force.
    assert soon["headroom_tokens"] == 100
    assert {e["id"]: e["headroom_tokens"] for e in ents}[rung["cheap"]] is None
    assert all("cost_per_mtok" in e for e in ents)


def test_replay_under_a_changed_tie_break_moves_a_decision_todays_ordering_kept(client, rung):
    """The sabotage target: replay must apply the PROPOSED tie-break, not the recorded one."""
    item = _item()
    assert _claim(rung["claimant"], item) == item
    same = _replay(tie_break="soonest_reset")
    assert (same["replayable"], same["moved"]) == (1, 0)
    out = _replay(tie_break="cheapest")
    assert (out["decisions"], out["replayable"], out["not_replayable"]) == (1, 1, 0)
    assert out["moved"] == 1 and out["held"] == 0
    (ex,) = out["examples"]
    assert ex["from"] == {"verdict": "claimed", "entitlement_id": rung["soon"]}
    assert ex["to"] == {"verdict": "claimed", "entitlement_id": rung["cheap"]}
    roomy = _replay(tie_break="most_headroom")
    assert roomy["moved"] == 1
    assert roomy["examples"][0]["to"]["entitlement_id"] == rung["roomy"]


def _sub(eid, vendor, created, rank, headroom):
    return {"id": eid, "vendor": vendor, "kind": "subscription", "rung": "subscription",
            "state": "available", "budget_spent": False, "reviewer_eligible": True,
            "next_reset_at": None, "headroom_tokens": headroom, "cost_per_mtok": None,
            "held": False, "created": created, "rank": rank}


def test_a_tie_the_proposal_cannot_break_falls_back_to_creation_order_not_the_recorded_rank():
    """No reset is stated, so soonest_reset ties all three; live, creation order then decides
    and A (created first) wins. Replay must agree, and so count the decision as moved — not
    inherit the recorded most_headroom ranking (B, C, A) from the order the snapshot lists."""
    from app.services import routing

    a, b, c = (_sub("A", "anthropic", 0, 2, 10), _sub("B", "openai", 1, 0, 100),
               _sub("C", "google", 2, 1, 50))
    policy = {"ladder": ["subscription", "api_key", "local"], "tie_break": "most_headroom",
              "when_exhausted": "fall_through", "subscription_order": None}
    snap = {"v": 1, "policy": policy, "declared": ["A", "B"], "entitlements": [b, c, a]}
    assert routing.choose(snap, policy) == ("claimed", "B")
    live = routing.order([a, b, c], ladder=policy["ladder"], tie_break="soonest_reset")
    assert [e["id"] for e in live] == ["A", "B", "C"]
    assert routing.choose(snap, {**policy, "tie_break": "soonest_reset"}) == ("claimed", "A")


def test_replay_reads_the_candidates_then_not_todays_headroom(client, world, rung):
    """After the claim, `cheap` runs out. Replay still moves the decision to it: then it had
    headroom, and that is the question replay answers."""
    item = _item()
    assert _claim(rung["claimant"], item) == item
    _report(client, world, rung["cheap"], rung["claimant"], "limit_hit", vendor_text="limit")
    out = _replay(tie_break="cheapest")
    assert out["moved"] == 1
    assert out["examples"][0]["to"]["entitlement_id"] == rung["cheap"]


def test_replay_counts_a_decision_hold_until_reset_would_have_held(client, spent_top):
    item = _item()
    assert _claim(spent_top["claimant"], item) == item  # fell through to the api key
    out = _replay(when_exhausted="hold_until_reset")
    assert out["replayable"] == 1 and out["moved"] == 1 and out["held"] == 1
    assert out["examples"][0]["to"] == {"verdict": "held", "entitlement_id": None}


def test_a_decision_recorded_before_candidates_is_not_replayable(client, rung):
    from app.db import SessionLocal
    from app.models import RoutingDecision

    item = _item()
    db = SessionLocal()
    try:
        db.add(RoutingDecision(item_id=item, kind="claim", entitlement_id=rung["soon"],
                               reason="legacy", candidates=None))
        db.commit()
    finally:
        db.close()
    out = _replay(tie_break="cheapest")
    assert (out["decisions"], out["replayable"], out["not_replayable"]) == (1, 0, 1)
    assert out["moved"] is None and out["held"] is None  # unknown, not "nothing moved"
    assert "nothing in the last 7 days is replayable" in out["reason"]

    # A replayable one beside it is counted on its own; the legacy row stays apart.
    item2 = _item("w2")
    assert _claim(rung["claimant"], item2) == item2
    out = _replay(tie_break="cheapest")
    assert (out["decisions"], out["replayable"], out["not_replayable"], out["moved"]) == (2, 1, 1, 1)
    assert out["reason"] is None


def test_decisions_outside_the_window_are_not_replayed(client, rung):
    from app.db import SessionLocal
    from app.models import RoutingDecision

    item = _item()
    assert _claim(rung["claimant"], item) == item
    db = SessionLocal()
    try:
        for r in db.query(RoutingDecision).all():
            r.created_at = NOW - timedelta(days=8)
        db.commit()
    finally:
        db.close()
    out = _replay(tie_break="cheapest")
    assert out["decisions"] == 0 and out["moved"] is None
    assert out["reason"] == "no routing decisions in the last 7 days"


def test_a_reservation_records_its_candidates_too(client, world):
    held = _ent(client, world, "anthropic")
    b = _runner(client, world, [held])
    _report(client, world, held, b, "registered")
    other = _ent(client, world, "openai")
    x = _runner(client, world, [other], vendor="openai")
    _report(client, world, other, x, "limit_hit", vendor_text="limit")
    _ent(client, world, "google")  # reviewer reserve; no runner declares it
    item = _item()
    assert _claim(x, item) is None
    rows = [r for r in _rows(item) if r[0] == "reservation"]
    assert rows and rows[0][2]["declared"] == [other]
    assert {e["id"]: e["held"] for e in rows[0][2]["entitlements"]}[held] is True


def test_replay_endpoint_validates_the_proposal(client, world, rung):
    owner = world["owner"]
    item = _item()
    assert _claim(rung["claimant"], item) == item
    url = f"/api/harness/capacity/replay?project_id={_P['id']}"
    r = client.get(url + "&tie_break=cheapest", headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 1 and r.json()["days"] == 7
    assert client.get(url + "&tie_break=fastest", headers=owner).status_code == 422
    assert client.get(url + "&ladder=api_key,moon", headers=owner).status_code == 422
    r = client.get(url + "&ladder=local", headers=owner)
    assert r.status_code == 200 and r.json()["moved"] == 1  # nothing on local: next rung
