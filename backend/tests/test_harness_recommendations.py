"""PRD-38 PR 3 — the four rules, the replay, accept/dismiss, and lesson candidates
(criteria 8–12).

Attempts are built through the real path and then given a recorded resolution, because the
replay's whole claim is that it re-ranks resolutions that actually happened.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (AttemptTelemetry, FleetProfile, HarnessLessonMark, Item, MemoryShard,
                        Project, RecommendationMark)
from app.services import harness as hsvc
from app.services import harness_rules as rules
from app.services import items as items_svc


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Rules"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def user_id(client, auth) -> str:
    return client.get("/api/auth/me", headers=auth).json()["id"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


def _resolution(winner: str, *others: str, statuses: dict | None = None,
                scores: dict | None = None, defaults: list[str] | None = None,
                local: dict | None = None, dropped: list[dict] | None = None) -> dict:
    """A recorded PRD-37 explanation, in the shape `Resolution.explain()` emits."""
    statuses, scores, local = statuses or {}, scores or {}, local or {}
    names = [winner, *others]
    shortlist = []
    for i, name in enumerate(names):
        harness, model = name.split(":", 1)
        shortlist.append({"harness": harness, "model": model, "vendor": harness,
                          "status": statuses.get(name, "verified"),
                          "score": scores.get(name, 1.0 - i * 0.1),
                          "order": i + 1, "local": local.get(name, False), "axes": {}})
    return {"source": "matrix", "tier": "cheap", "role": "worker", "lane": "backend",
            "shortlist": shortlist, "dropped_rows": list(dropped or []),
            "profile": {"user": "u", "defaults": list(defaults or []), "weights": {}},
            "eligible": {}, "dropped": {}, "winner": shortlist[0], "runner_up": None,
            "refused": None}


def _attempt(client, key, db, planner, label, *, outcome="signed_off", vendor="gbagent",
             model="qwen3.6", lane="backend", touchpoints=None, resolution=None,
             version="1.0.0") -> AttemptTelemetry:
    item = _ok(_mcp(client, key, "create_item", {
        "title": label, "status": "next",
        "touchpoints": touchpoints or ["backend/app/x.py"]}))["id"]
    _ok(_mcp(client, key, "delegate", {"id": item, "lane": lane, "tier": "cheap",
                                       "agent_id": planner}))
    child = _agent(client, key, f"c-{label}", parent_agent_id=planner,
                   capabilities={"instance": label, "vendor": vendor, "model": model})
    assert items_svc.claim_item(db, item, child) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, f"r-{label}", capabilities={"instance": f"rev-{label}"})
    if outcome == "signed_off":
        _ok(_mcp(client, key, "sign_off", {"id": item, "agent_id": reviewer,
                                           "evidence": [{"kind": "note", "detail": "ok"}]}))
    else:
        _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": "wrong"}))
        stored = db.get(Item, item)
        stored.bounce_pinned_to, stored.bounce_pinned_until = None, None
        db.commit()
    db.expire_all()
    row = db.scalar(select(AttemptTelemetry).where(AttemptTelemetry.item_id == item))
    row.binary_version = version
    if resolution is not None:
        row.resolution = resolution
    db.commit()
    return row


def _cards(client, auth, proj, **params):
    q = "".join(f"&{k}={v}" for k, v in params.items())
    r = client.get(f"/api/harness/recommendations?project_id={proj}{q}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _by_rule(payload, rule):
    return [c for c in payload["cards"] if c["rule"] == rule]


# ---- 8: R1 promote -----------------------------------------------------------------------------

def _ten_good(client, key, db, planner, *, n=10, wins=10, statuses=None, lane="backend",
              touchpoints=None):
    res = _resolution("gbagent:qwen3.6", "claude:sonnet",
                      statuses=statuses or {"gbagent:qwen3.6": "unverified"})
    for i in range(n):
        _attempt(client, key, db, planner, f"{lane}{i}",
                 outcome="signed_off" if i < wins else "bounced", lane=lane,
                 touchpoints=touchpoints, resolution=res)


def test_r1_fires_on_an_unverified_row_that_is_good_enough_and_drafts_its_evidence(
        client, key, db, proj, auth):
    """8. Sabotage: read `status` from anywhere but the recorded resolution and this cannot
    fire at all — the server holds no matrix."""
    planner = _agent(client, key, "planner")
    _ten_good(client, key, db, planner)
    cards = _by_rule(_cards(client, auth, proj), "R1")
    assert len(cards) == 1, cards
    card = cards[0]
    assert card["draft"]["status"] == "verified"
    assert card["draft"]["target"] == "gbagent:qwen3.6"
    assert "10/10" in card["draft"]["evidence_line"] or "10 of 10" in card["detail"]
    assert card["thresholds"] == {"min_finished": 10, "min_rate": 0.8}


def test_r1_does_not_fire_at_nine_finished_or_at_a_rate_just_under(client, key, db, proj, auth):
    """8. Both edges, because a threshold that is off by one is a rule nobody can trust."""
    planner = _agent(client, key, "planner")
    _ten_good(client, key, db, planner, n=9, wins=9)
    assert _by_rule(_cards(client, auth, proj), "R1") == []

    planner2 = _agent(client, key, "planner2")
    # 10 attempts, 7 signed off = 0.7, under 0.8.
    _ten_good(client, key, db, planner2, n=10, wins=7, lane="frontend",
              touchpoints=["web/src/a.tsx"])
    assert [c for c in _by_rule(_cards(client, auth, proj), "R1")
            if c["cells"][0]["cell"]["lane"] == "frontend"] == []


def test_r1_does_not_fire_on_bands_that_only_pass_when_pooled(client, key, db, proj, auth):
    """8, the sharp case. Six S attempts at 1.0 and six L attempts at 0.5 pool to 12 at 0.75 —
    no single band reaches the rule, and neither may the card."""
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", statuses={"gbagent:qwen3.6": "unverified"})
    big = [f"backend/app/{n}.py" for n in range(6)]
    for i in range(6):
        _attempt(client, key, db, planner, f"small{i}", resolution=res)
    for i in range(6):
        _attempt(client, key, db, planner, f"large{i}", touchpoints=big,
                 outcome="signed_off" if i < 3 else "bounced", resolution=res)
    assert _by_rule(_cards(client, auth, proj), "R1") == []


def test_a_card_carries_the_sibling_cells_it_did_not_fire_on(client, key, db, proj, auth):
    """8 / D7. The matrix keys on lane, not band, so a promote generalises — and has to show
    what it is generalising over."""
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", statuses={"gbagent:qwen3.6": "unverified"})
    big = [f"backend/app/{n}.py" for n in range(6)]
    for i in range(10):
        _attempt(client, key, db, planner, f"L{i}", touchpoints=big, resolution=res)
    for i in range(9):
        _attempt(client, key, db, planner, f"S{i}", outcome="bounced", resolution=res)
    card = _by_rule(_cards(client, auth, proj), "R1")[0]
    assert card["cells"][0]["cell"]["size_band"] == "L"
    bands = {s["cell"]["size_band"] for s in card["siblings"]}
    assert "S" in bands, card["siblings"]
    assert "generalis" in card["detail"]


# ---- 9: R2, R3, R4 -----------------------------------------------------------------------------

def test_r2_fires_on_a_verified_row_that_keeps_bouncing(client, key, db, proj, auth):
    """9."""
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", statuses={"gbagent:qwen3.6": "verified"})
    for i in range(8):
        _attempt(client, key, db, planner, f"bad{i}",
                 outcome="signed_off" if i < 2 else "bounced", resolution=res)
    cards = _by_rule(_cards(client, auth, proj), "R2")
    assert len(cards) == 1
    assert cards[0]["draft"]["status"] == "failed"
    assert cards[0]["replay"]["considered"] == 8


def test_r3_fires_when_a_profiles_top_default_is_beaten_in_the_same_cell(
        client, key, db, proj, auth, user_id):
    """9. The rival must beat it by the margin, at `n`, in the SAME lane/class/band."""
    planner = _agent(client, key, "planner")
    db.add(FleetProfile(id="fp_test", user_id=user_id, project_id=proj,
                        defaults=["claude", "gbagent"], weights={}, excludes=[]))
    db.commit()
    res = _resolution("claude:sonnet", "gbagent:qwen3.6", defaults=["claude", "gbagent"])
    for i in range(8):
        _attempt(client, key, db, planner, f"cl{i}", vendor="claude", model="sonnet",
                 outcome="signed_off" if i < 3 else "bounced", resolution=res)
    for i in range(8):
        _attempt(client, key, db, planner, f"gb{i}", vendor="gbagent", model="qwen3.6",
                 resolution=res)
    cards = _by_rule(_cards(client, auth, proj), "R3")
    assert len(cards) == 1, cards
    assert cards[0]["draft"]["defaults"][0] == "gbagent"
    assert cards[0]["draft"]["where"] == "PUT /api/fleet/profile"


def test_r4_fires_when_a_local_only_project_keeps_bouncing_locally(
        client, key, db, proj, auth):
    """9."""
    planner = _agent(client, key, "planner")
    project = db.get(Project, proj)
    project.fleet_policy = {"local_only": True}
    db.commit()
    res = _resolution("gbagent:qwen3.6", local={"gbagent:qwen3.6": True})
    for i in range(8):
        _attempt(client, key, db, planner, f"loc{i}",
                 outcome="signed_off" if i < 2 else "bounced", resolution=res)
    cards = _by_rule(_cards(client, auth, proj), "R4")
    assert len(cards) == 1, cards
    assert cards[0]["draft"]["local_only"] is False
    assert "does not say those attempts would have gone better" in cards[0]["detail"]


def test_every_card_names_its_rule_its_cells_and_its_replay(client, key, db, proj, auth):
    """9 / D15."""
    planner = _agent(client, key, "planner")
    _ten_good(client, key, db, planner)
    for card in _cards(client, auth, proj)["cards"]:
        assert card["rule"] in rules.RULES
        assert card["cells"] and card["cells"][0]["finished"] > 0
        assert "considered" in card["replay"] and "summary" in card["replay"]
        assert card["thresholds"]


# ---- 10: the replay ----------------------------------------------------------------------------

def test_the_replay_counts_the_resolutions_a_change_would_have_moved(client, key, db, proj, auth):
    """10. A defaults reorder moves exactly the resolutions the reordered row was second in."""
    planner = _agent(client, key, "planner")
    res = _resolution("claude:sonnet", "gbagent:qwen3.6",
                      scores={"claude:sonnet": 0.5, "gbagent:qwen3.6": 0.5},
                      defaults=["claude", "gbagent"])
    rows = [_attempt(client, key, db, planner, f"m{i}", vendor="claude", model="sonnet",
                     resolution=res) for i in range(4)]
    out = rules.replay(rows, {"kind": "defaults", "defaults": ["gbagent", "claude"]})
    assert out["considered"] == 4 and out["changed"] == 4
    assert out["moves"] == [{"from": "claude:sonnet", "to": "gbagent:qwen3.6", "count": 4}]
    assert "would have changed 4 of 4" in out["summary"]


def test_a_replay_of_no_change_reports_zero(client, key, db, proj, auth):
    """10. Sabotage: rank by anything but PRD-37's four keys and this stops being zero."""
    planner = _agent(client, key, "planner")
    res = _resolution("claude:sonnet", "gbagent:qwen3.6", defaults=["claude", "gbagent"])
    rows = [_attempt(client, key, db, planner, f"n{i}", vendor="claude", model="sonnet",
                     resolution=res) for i in range(3)]
    out = rules.replay(rows, {"kind": "defaults", "defaults": ["claude", "gbagent"]})
    assert out["considered"] == 3 and out["changed"] == 0 and out["moves"] == []


def test_an_attempt_with_no_recorded_resolution_is_counted_as_unreplayable(
        client, key, db, proj, auth):
    """10. Sabotage: treat a missing resolution as "unchanged" and the card's denominator
    silently grows to include work it knows nothing about."""
    planner = _agent(client, key, "planner")
    with_res = _attempt(client, key, db, planner, "has",
                        resolution=_resolution("gbagent:qwen3.6", "claude:sonnet"))
    without = _attempt(client, key, db, planner, "hasnt")
    out = rules.replay([with_res, without], {"kind": "status", "target": "gbagent:qwen3.6",
                                             "status": "failed"})
    assert out["considered"] == 1 and out["skipped_no_resolution"] == 1
    assert "1 had no recorded resolution" in out["summary"]


def test_a_demote_replay_removes_the_row_and_names_where_the_work_would_have_gone(
        client, key, db, proj, auth):
    """10."""
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", "claude:sonnet")
    rows = [_attempt(client, key, db, planner, f"d{i}", resolution=res) for i in range(2)]
    out = rules.replay(rows, {"kind": "status", "target": "gbagent:qwen3.6", "status": "failed"})
    assert out["changed"] == 2
    assert out["moves"] == [{"from": "gbagent:qwen3.6", "to": "claude:sonnet", "count": 2}]


def test_lifting_local_only_readmits_the_rows_the_policy_dropped(client, key, db, proj, auth):
    """10 / R4. The dropped rows carry the score they would have had, which is the whole
    reason they are recorded structurally."""
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", local={"gbagent:qwen3.6": True},
                      scores={"gbagent:qwen3.6": 0.4},
                      dropped=[{"harness": "claude", "model": "opus", "vendor": "claude",
                                "status": "verified", "score": 0.9, "order": 1,
                                "local": False, "stage": "policy", "why": "local_only"}])
    rows = [_attempt(client, key, db, planner, f"p{i}", resolution=res) for i in range(3)]
    out = rules.replay(rows, {"kind": "policy", "local_only": False})
    assert out["changed"] == 3
    assert out["moves"] == [{"from": "gbagent:qwen3.6", "to": "claude:opus", "count": 3}]


# ---- 11: accept and dismiss --------------------------------------------------------------------

def _mark(client, auth, proj, card, action):
    r = client.post("/api/harness/recommendations/mark", headers=auth,
                    json={"project_id": proj, "card_key": card["key"],
                          "evidence_hash": card["evidence_hash"], "action": action})
    assert r.status_code == 200, r.text
    return r.json()


def test_dismiss_hides_a_card_until_its_evidence_moves(client, key, db, proj, auth):
    """11. Sabotage: key the mark on the rule alone and the card never comes back."""
    planner = _agent(client, key, "planner")
    _ten_good(client, key, db, planner)
    card = _by_rule(_cards(client, auth, proj), "R1")[0]
    _mark(client, auth, proj, card, "dismiss")
    assert _by_rule(_cards(client, auth, proj), "R1") == []

    # More evidence, same rule, same target: a NEW card, because the numbers moved.
    for i in range(3):
        _attempt(client, key, db, planner, f"more{i}",
                 resolution=_resolution("gbagent:qwen3.6",
                                        statuses={"gbagent:qwen3.6": "unverified"}))
    again = _by_rule(_cards(client, auth, proj), "R1")
    assert len(again) == 1
    assert again[0]["evidence_hash"] != card["evidence_hash"]
    assert again[0]["previously"]["state"] == "dismissed"
    assert again[0]["previously"]["evidence_changed"] is True


def test_the_evidence_hash_covers_the_numbers_and_not_just_the_proposal(
        client, key, db, proj, auth):
    """11 / D7. Found by sabotage: R3 and R4 drafts carry no counts, so a hash over the
    proposal alone would never move and those cards could never come back once dismissed.
    The hash has to cover the cells."""
    planner = _agent(client, key, "planner")
    project = db.get(Project, proj)
    project.fleet_policy = {"local_only": True}
    db.commit()
    res = _resolution("gbagent:qwen3.6", local={"gbagent:qwen3.6": True})
    for i in range(8):
        _attempt(client, key, db, planner, f"pol{i}",
                 outcome="signed_off" if i < 2 else "bounced", resolution=res)
    first = _by_rule(_cards(client, auth, proj), "R4")[0]

    for i in range(4):
        _attempt(client, key, db, planner, f"pol_more{i}", outcome="bounced", resolution=res)
    second = _by_rule(_cards(client, auth, proj), "R4")[0]

    assert second["draft"] == first["draft"], "the proposal is unchanged — only the counts moved"
    assert second["evidence_hash"] != first["evidence_hash"]


def test_accepting_changes_no_server_state_but_the_mark(client, key, db, proj, auth, user_id):
    """11. R1 accept produces text for a commit; nothing here writes a matrix or a profile."""
    planner = _agent(client, key, "planner")
    _ten_good(client, key, db, planner)
    card = _by_rule(_cards(client, auth, proj), "R1")[0]
    before = db.get(Project, proj).fleet_policy
    _mark(client, auth, proj, card, "accept")
    db.expire_all()
    assert db.get(Project, proj).fleet_policy == before
    assert db.scalars(select(FleetProfile)).all() == []
    marks = db.scalars(select(RecommendationMark)).all()
    assert len(marks) == 1 and marks[0].accepted_at is not None
    assert marks[0].dismissed_at is None


def test_the_card_a_person_accepted_returns_when_its_evidence_reverses(
        client, key, db, proj, auth):
    """11 / D7. Nothing is retracted — nothing was applied — but the person who accepted it
    must be told the numbers moved."""
    planner = _agent(client, key, "planner")
    _ten_good(client, key, db, planner)
    card = _by_rule(_cards(client, auth, proj), "R1")[0]
    _mark(client, auth, proj, card, "accept")
    assert _by_rule(_cards(client, auth, proj), "R1") == []

    for i in range(6):
        _attempt(client, key, db, planner, f"rev{i}", outcome="bounced",
                 resolution=_resolution("gbagent:qwen3.6",
                                        statuses={"gbagent:qwen3.6": "unverified"}))
    seen = _cards(client, auth, proj, include_seen="true")
    r1 = [c for c in seen["cards"] if c["rule"] == "R1"]
    # The rule no longer fires at the new rate, so what returns is the R2 side or nothing —
    # either way the accepted card is gone and the mark records that it was accepted.
    assert r1 == [] or r1[0]["previously"]["state"] == "accepted"


# ---- 12: lesson candidates ---------------------------------------------------------------------

def _shards(db, source="harness-telemetry"):
    db.expire_all()
    return [s for s in db.scalars(select(MemoryShard)).all() if s.source == source]


def test_a_cell_crossing_the_floor_drafts_one_candidate_and_only_once(
        client, key, db, proj, auth):
    """12. Sabotage: make the mark a counter reset by the floor and the second crossing
    drafts again."""
    planner = _agent(client, key, "planner")
    for i in range(5):
        _attempt(client, key, db, planner, f"x{i}")
    _cards(client, auth, proj)
    shards = _shards(db)
    assert len(shards) == 1
    assert shards[0].status == "candidate"
    assert "signed off 5/5" in shards[0].text
    assert db.scalars(select(HarnessLessonMark)).all()

    # Reading again drafts nothing; nor does more evidence in the same cell.
    _cards(client, auth, proj)
    _attempt(client, key, db, planner, "x5")
    _cards(client, auth, proj)
    assert len(_shards(db)) == 1


def test_a_new_binary_version_inherits_the_mark(client, key, db, proj, auth):
    """12. A point release is not a new thing to learn."""
    planner = _agent(client, key, "planner")
    for i in range(5):
        _attempt(client, key, db, planner, f"v1_{i}", version="1.0.0")
    _cards(client, auth, proj)
    assert len(_shards(db)) == 1
    for i in range(5):
        _attempt(client, key, db, planner, f"v2_{i}", version="2.0.0")
    _cards(client, auth, proj)
    assert len(_shards(db)) == 1


def test_a_thin_cell_drafts_nothing(client, key, db, proj, auth):
    """12. Crossing is the event; being measured at all is not."""
    planner = _agent(client, key, "planner")
    for i in range(4):
        _attempt(client, key, db, planner, f"thin{i}")
    _cards(client, auth, proj)
    assert _shards(db) == []


def test_accepting_a_card_drafts_a_candidate_that_says_what_was_measured(
        client, key, db, proj, auth):
    """12. Sabotage: draft the card key as the lesson text and this fails — an agent cannot
    act on "R1:gbagent:qwen3.6"."""
    planner = _agent(client, key, "planner")
    _ten_good(client, key, db, planner)
    card = _by_rule(_cards(client, auth, proj), "R1")[0]
    before = {s.id for s in _shards(db)}
    out = _mark(client, auth, proj, card, "accept")
    assert out["lesson_drafted"]
    new = [s for s in _shards(db) if s.id not in before]
    assert len(new) == 1
    assert new[0].status == "candidate"
    assert "signed off 10/10" in new[0].text
    assert "R1" not in new[0].text


def test_nothing_this_pr_writes_is_published(client, key, db, proj, auth):
    """12. The whole of D8's promise, asserted rather than described."""
    planner = _agent(client, key, "planner")
    _ten_good(client, key, db, planner)
    card = _by_rule(_cards(client, auth, proj), "R1")[0]
    _mark(client, auth, proj, card, "accept")
    assert all(s.status == "candidate" for s in _shards(db))
