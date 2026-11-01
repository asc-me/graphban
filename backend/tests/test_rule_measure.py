"""PRD-P63 S5 (GRPH-1265): rule measurement and the opt-in platform aggregates.

Pins the status contract (`measured` | `insufficient` | `unavailable`, a real zero is a zero,
null never means "not enough data"), escape rates before/after adoption with n, retirement,
and the D13 floors on platform rates: opt-in, three orgs, n, top-org share, no ids.
"""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import Event, Observation, Organization, PlatformRuleRollup, Project
from app.services import rule_measure as M
from app.services import rule_toggles as T
from app.services import rules as R

RULE = "review.sabotage_required"
PID = "core"


@pytest.fixture()
def db(client):
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture()
def library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="review", title="Sabotage required", summary="s", enforcement="enforced",
        enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=False, default_params={"min_tests_failed": 1}))


def _ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def _adopt(db, days_ago, on=True, pid=PID):
    db.add(Event(ts=_ago(days_ago), actor_type="user", surface="rest", action="set_rule_toggle",
                 target_type="rule", target_id=RULE, project_id=pid,
                 meta={"scope": "project", "effective_after": {"on": on}}))
    db.commit()


def _signoffs(db, n, days_ago, tag, pid=PID):
    for i in range(n):
        db.add(Event(ts=_ago(days_ago), actor_type="apikey", surface="mcp", action="sign_off",
                     target_type="item", target_id=f"{tag}-{i}", project_id=pid))
    db.commit()


def _obs(db, kind, days_ago, key, rule_id=None, pid=PID):
    db.add(Observation(project_id=pid, kind=kind, stage="merge", rule_id=rule_id,
                       dedupe_key=key, occurred_at=_ago(days_ago)))
    db.commit()


def test_value_contract():
    assert M.value(0, n=5, min_n=3)["status"] == "measured"
    assert M.value(0, n=5, min_n=3)["value"] == 0
    ins = M.value(0.5, n=2, min_n=10)
    assert ins["status"] == "insufficient" and ins["value"] is None
    assert ins["reason"] == "not enough data (2 of 10)"
    un = M.value(None, n=0, min_n=1, available=False, reason="why")
    assert un["status"] == "unavailable" and un["reason"] == "why"


def test_not_adopted_is_unavailable_not_zero(db, library):
    r = M.project_effect(db, PID)["rules"][0]
    assert r["escape_rate"]["after"]["status"] == "unavailable"
    assert r["caught"]["status"] == "measured" and r["caught"]["value"] == 0
    assert r["retirement"]["suggested"] is False


def test_escape_rate_before_after_with_n(db, library):
    _adopt(db, 10)
    _signoffs(db, 10, 15, "b")
    _signoffs(db, 12, 5, "a")
    for i in range(5):
        _obs(db, "ci_red_after_sign_off", 14, f"b{i}")
    _obs(db, "reopened_after_done", 4, "a0")
    r = M.project_effect(db, PID)["rules"][0]["escape_rate"]
    assert r["before"] == {"status": "measured", "value": 0.5, "n": 10, "n_band": None,
                           "min_n": 10, "reason": None}
    assert r["after"]["status"] == "measured" and r["after"]["n"] == 12
    assert r["after"]["value"] == round(1 / 12, 4)


def test_escape_rate_insufficient_not_a_number(db, library):
    _adopt(db, 10)
    _signoffs(db, 3, 5, "a")
    r = M.project_effect(db, PID)["rules"][0]["escape_rate"]["after"]
    assert r["status"] == "insufficient" and r["value"] is None and r["n"] == 3


def test_bounce_counts_only_after_a_prior_signoff(db, library):
    _adopt(db, 10)
    _signoffs(db, 10, 5, "a")
    _obs(db, "bounce", 3, "bn0")  # no item: not an escape
    db.add(Observation(project_id=PID, item_id=None, kind="bounce", stage="review",
                       dedupe_key="bn1", occurred_at=_ago(3)))
    db.commit()
    assert M.project_effect(db, PID)["rules"][0]["escape_rate"]["after"]["value"] == 0


def _bug_and_work(db, signed=True):
    from app.services import items as items_svc
    work = _item(db, "shipped work")
    work.status, work.reviewed_by = ("done", "GRPH-R") if signed else (work.status, None)
    bug = items_svc.create_item(db, title="it broke", effort=1, project_id=PID, tags=["bug"])
    db.commit()
    return bug, work


def test_bug_linked_to_signed_off_work_is_an_escape_and_leaves_unmeasured(db, library):
    from app.services import observations as O
    _adopt(db, 10)
    _signoffs(db, 10, 5, "a")
    bug, work = _bug_and_work(db)
    assert O.record_bug_escape(db, bug, work) is True
    assert O.record_bug_escape(db, bug, work) is False  # one fact per pair
    cell = M.project_effect(db, PID)["rules"][0]
    assert cell["escapes_unmeasured"] == []
    assert cell["escape_rate"]["after"]["value"] == 0.1
    o = db.query(Observation).filter_by(kind="bug_escaped").one()
    assert o.item_id == work.id and o.ref == {"bug_item_id": bug.id}


def test_bug_escape_before_adoption_counts_in_the_before_window(db, library):
    from app.services import observations as O
    _adopt(db, 10)
    _signoffs(db, 10, 15, "b")
    bug, work = _bug_and_work(db)
    bug.created_at = _ago(14)
    db.commit()
    O.record_bug_escape(db, bug, work)
    r = M.project_effect(db, PID)["rules"][0]["escape_rate"]
    assert r["before"]["value"] == 0.1


def test_bug_escape_refuses_what_is_not_an_escape(db, library):
    from app.services import observations as O
    bug, work = _bug_and_work(db, signed=False)
    with pytest.raises(O.BugEscapeRefused, match="signed off"):
        O.record_bug_escape(db, bug, work)
    bug, work = _bug_and_work(db)
    with pytest.raises(O.BugEscapeRefused, match="itself"):
        O.record_bug_escape(db, work, work)
    with pytest.raises(O.BugEscapeRefused, match="not tagged"):
        O.record_bug_escape(db, work, bug)


def test_caught_counts_refusals_naming_the_rule(db, library):
    _obs(db, "refusal", 1, "r1", rule_id=RULE)
    _obs(db, "refusal", 1, "r2", rule_id="other.rule")
    c = M.project_effect(db, PID)["rules"][0]["caught"]
    assert c["value"] == 1 and c["status"] == "measured"


def _item(db, title):
    from app.services import items as items_svc
    it = items_svc.create_item(db, title=title, effort=1, project_id=PID)
    db.commit()
    return it


def test_cost_with_no_data_is_insufficient_not_zero(db, library):
    cost = M.project_effect(db, PID)["rules"][0]["cost"]
    for k in ("overridden", "held_seconds"):
        assert cost[k]["status"] == "insufficient" and cost[k]["value"] is None
        assert cost[k]["n"] == 0 and cost[k]["min_n"] == M.MIN_COST_N


def test_override_is_recorded_naming_rule_item_actor(db, library):
    it = _item(db, "ov")
    assert M.record_override(db, it, RULE, "user-1")
    assert M.record_override(db, it, RULE, "user-2")  # a second override is a second fact
    db.commit()
    rows = db.query(Observation).filter_by(kind=M.OVERRIDE_KIND).all()
    assert len(rows) == 2
    assert {(r.rule_id, r.item_id, r.ref["actor"]) for r in rows} == {
        (RULE, it.id, "user-1"), (RULE, it.id, "user-2")}


def test_overridden_measured_against_refusals(db, library):
    it = _item(db, "ov2")
    for i in range(M.MIN_COST_N):
        _obs(db, "refusal", 1, f"r{i}", rule_id=RULE)
    cost = M.project_effect(db, PID)["rules"][0]["cost"]
    assert cost["overridden"]["status"] == "measured" and cost["overridden"]["value"] == 0
    M.record_override(db, it, RULE, "user-1")
    db.commit()
    o = M.project_effect(db, PID)["rules"][0]["cost"]["overridden"]
    assert o["value"] == 1 and o["n"] == M.MIN_COST_N
    # Filtered to the one rule, the same refusals stand behind it: still measured.
    other = M.project_effect(db, PID, RULE)["rules"][0]
    assert other["cost"]["overridden"]["status"] == "measured"


def test_overridden_thin_refusals_insufficient(db, library):
    it = _item(db, "ov3")
    _obs(db, "refusal", 1, "r1", rule_id=RULE)
    M.record_override(db, it, RULE, "u")
    db.commit()
    o = M.project_effect(db, PID)["rules"][0]["cost"]["overridden"]
    assert o["status"] == "insufficient" and o["n"] == 1 and o["value"] is None


def test_held_time_recorded_per_rule_and_measured(db, library):
    t0 = _ago(1)
    for i in range(M.MIN_COST_N):
        it = _item(db, f"h{i}")
        assert M.record_held(db, it, RULE, t0, t0 + timedelta(seconds=60))
        assert not M.record_held(db, it, RULE, t0, t0 + timedelta(seconds=60))  # same hold
    db.commit()
    h = M.project_effect(db, PID)["rules"][0]["cost"]["held_seconds"]
    assert h["status"] == "measured" and h["value"] == 60 * M.MIN_COST_N
    assert h["n"] == M.MIN_COST_N


def test_held_thin_and_bad_interval(db, library):
    it = _item(db, "h-thin")
    t0 = _ago(1)
    M.record_held(db, it, RULE, t0, t0 + timedelta(seconds=30))
    db.commit()
    h = M.project_effect(db, PID)["rules"][0]["cost"]["held_seconds"]
    assert h["status"] == "insufficient" and h["n"] == 1 and h["value"] is None
    with pytest.raises(ValueError):
        M.record_held(db, it, RULE, t0, t0 - timedelta(seconds=1))


def test_retirement_after_long_window_with_no_catches(db, library):
    _adopt(db, 90)
    assert M.project_effect(db, PID)["rules"][0]["retirement"]["suggested"] is True
    _obs(db, "refusal", 1, "r1", rule_id=RULE)
    assert M.project_effect(db, PID)["rules"][0]["retirement"]["suggested"] is False


def test_adopted_then_dropped_is_not_adopted(db, library):
    _adopt(db, 20)
    _adopt(db, 10, on=False)
    assert M.adopted_at(db, RULE, PID) is None


@pytest.fixture()
def default_on(library):
    T.LIBRARY[RULE] = dataclasses.replace(T.LIBRARY[RULE], default_on=True)


def test_default_on_rule_never_toggled_counts_as_on_with_no_adoption_time(db, default_on):
    # On by default, no toggle row: it is on, so it counts, and every measure that needs an
    # adoption time says it is unavailable and why. It never vanishes from the scope.
    assert M.adoption(db, RULE, PID) == (True, None)
    r = M.project_effect(db, PID)["rules"][0]
    assert r["projects_on"] == 1 and r["projects_on_without_adoption_time"] == 1
    for side in ("before", "after"):
        assert r["escape_rate"][side]["status"] == "unavailable"
        assert r["escape_rate"][side]["reason"] == M.NO_ADOPTION_TIME
    assert r["retirement"]["status"] == "unavailable" and r["retirement"]["suggested"] is False
    o, pid = _org(db, "dflt", False)
    assert M.contribution(db, o.id)[RULE]["projects_on"] == 1


def test_default_on_turned_off_then_on_is_measured_from_the_turn(db, default_on):
    _adopt(db, 20, on=False)
    _adopt(db, 10)
    on, since = M.adoption(db, RULE, PID)
    assert on and since is not None
    assert M.project_effect(db, PID)["rules"][0]["projects_on_without_adoption_time"] == 0


# ---- platform ---------------------------------------------------------------------------------

def _org(db, name, share, plan="team"):
    o = Organization(id=f"org-{name}", name=name, plan=plan, telemetry_share=share)
    db.add(o)
    db.flush()
    p = Project(id=f"p-{o.id}", name=name, tag=name[:3].upper(), org_id=o.id)
    db.add(p)
    db.commit()
    return o, p.id


def _seed(db, pid, before, after):
    _adopt(db, 10, pid=pid)
    _signoffs(db, before, 15, f"{pid}-b", pid=pid)
    _signoffs(db, after, 5, f"{pid}-a", pid=pid)


@pytest.fixture()
def hosted(monkeypatch):
    monkeypatch.setattr(settings, "hosted_mode", True)


def test_platform_served_only_above_floors_and_without_ids(db, library, hosted):
    orgs = [_org(db, f"o{i}", True) for i in range(3)]
    for _, pid in orgs:
        _seed(db, pid, 10, 10)
    M.platform_roll(db)
    db.commit()
    out = M.platform_effect(db, orgs[0][0].id)
    assert out["available"] is True
    cell = out["rules"][0]
    assert cell["before"]["status"] == "measured"
    assert cell["after"]["n"] is None and cell["after"]["n_band"] == "20–49"
    blob = repr(out)
    for o, pid in orgs:
        assert o.id not in blob and pid not in blob


def test_platform_below_n_floor_shows_a_band_never_the_count(db, library, hosted):
    # Three orgs and no dominant one, but only 3 × 6 = 18 sign-offs before adoption: below 20.
    # The exact 18 would let a contributor subtract its own and recover the others' (D13).
    orgs = [_org(db, f"o{i}", True) for i in range(3)]
    for _, pid in orgs:
        _seed(db, pid, 6, 10)
    M.platform_roll(db)
    cell = M.platform_effect(db, orgs[0][0].id)["rules"][0]
    assert cell["before"]["status"] == "insufficient" and cell["before"]["value"] is None
    for side in ("before", "after"):
        assert cell[side]["n"] is None
    assert cell["before"]["n_band"] == "<20" and cell["after"]["n_band"] == "20–49"
    assert "18" not in repr(cell) and "30" not in repr(cell)


def test_platform_short_side_is_named_and_the_other_stays_measured(db, library, hosted):
    # Before is short (18), after meets the floor (30): only before says "not enough data",
    # and it names itself; after is a real rate.
    orgs = [_org(db, f"o{i}", True) for i in range(3)]
    for _, pid in orgs:
        _seed(db, pid, 6, 10)
    M.platform_roll(db)
    cell = M.platform_effect(db, orgs[0][0].id)["rules"][0]
    assert "before adoption" in cell["before"]["reason"]
    assert "one side" not in cell["before"]["reason"]
    assert cell["after"]["status"] == "measured" and cell["after"]["value"] == 0
    assert cell["after"]["reason"] is None


def test_platform_n_is_never_an_int_on_any_branch(db, library, hosted):
    orgs = [_org(db, f"o{i}", True) for i in range(2)]
    for _, pid in orgs:
        _seed(db, pid, 10, 10)
    M.platform_roll(db)
    cell = M.platform_effect(db, orgs[0][0].id)["rules"][0]
    assert cell["before"]["n"] is None and cell["before"]["n_band"] is None
    assert set(cell["before"]) == set(M.value(0, n=1, min_n=1))


def test_project_scope_n_is_exact_and_escape_scope_is_project_wide(db, library):
    _adopt(db, 10)
    _signoffs(db, 12, 15, "b")
    cell = M.project_effect(db, PID)["rules"][0]
    assert cell["escape_rate"]["before"]["n"] == 12 and cell["escape_rate"]["before"]["n_band"] is None
    assert cell["escape_scope"] == "project" and "stage" in cell["escape_scope_note"]


def test_platform_needs_three_orgs(db, library, hosted):
    orgs = [_org(db, f"o{i}", True) for i in range(2)]
    for _, pid in orgs:
        _seed(db, pid, 10, 10)
    M.platform_roll(db)
    cell = M.platform_effect(db, orgs[0][0].id)["rules"][0]
    assert cell["after"]["status"] == "unavailable" and "fewer than 3 orgs" in cell["after"]["reason"]


def test_platform_top_org_share_cap(db, library, hosted):
    big, bp = _org(db, "big", True)
    _seed(db, bp, 100, 100)
    for i in range(2):
        _, pid = _org(db, f"s{i}", True)
        _seed(db, pid, 10, 10)
    M.platform_roll(db)
    cell = M.platform_effect(db, big.id)["rules"][0]
    assert cell["after"]["status"] == "unavailable" and "share" in cell["after"]["reason"]


def test_non_contributing_org_contributes_nothing_and_sees_nothing(db, library, hosted):
    orgs = [_org(db, f"o{i}", True) for i in range(3)]
    out_org, out_pid = _org(db, "out", False)
    for _, pid in orgs + [(out_org, out_pid)]:
        _seed(db, pid, 10, 10)
    M.platform_roll(db)
    row = db.get(PlatformRuleRollup, (datetime.now(timezone.utc).strftime("%G-W%V"), RULE))
    assert row.projects_on == 3
    seen = M.platform_effect(db, out_org.id)
    assert seen["available"] is False and "opted in" in seen["reason"]


def test_opting_out_purges_on_next_roll(db, library, hosted):
    orgs = [_org(db, f"o{i}", True) for i in range(3)]
    for _, pid in orgs:
        _seed(db, pid, 10, 10)
    M.platform_roll(db)
    orgs[0][0].telemetry_share = False
    db.commit()
    M.platform_roll(db)
    cell = M.platform_effect(db, orgs[1][0].id)["rules"][0]
    assert cell["after"]["status"] == "unavailable"


def test_self_hosted_has_no_platform_rates(db, library, monkeypatch):
    monkeypatch.setattr(settings, "hosted_mode", False)
    o, _ = _org(db, "x", True)
    assert M.platform_effect(db, o.id)["available"] is False


def test_org_and_platform_are_plan_gated(db, library, hosted):
    from app.services.quotas import QuotaExceeded

    o, _ = _org(db, "free", True, plan="free")
    with pytest.raises(QuotaExceeded):
        M.org_effect(db, o.id)
    with pytest.raises(QuotaExceeded):
        M.platform_effect(db, o.id)


# ---- the real paths: a refusal, then the rule passing or a person overriding it (GRPH-1283) ----

from tests.test_fleet_adversarial_gate import (  # noqa: E402,F401  (fixtures + helpers)
    SABOTAGE, _ok, _ready_for_review, _rpc, key, proj)


def _refused_signoff(client, key, item, reviewer):
    err = _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})
    assert RULE in err["structuredContent"]["error"]["message"]


def _effect_cost(client, auth, proj):
    r = client.get(f"/api/rules/effect?project_id={proj}&rule_id={RULE}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["rules"][0]["cost"]


def _switch_off(client, auth, proj):
    r = client.put(f"/api/rules/toggles/{RULE}?project_id={proj}", headers=auth,
                   json={"on": False, "reason": "holding too much work"})
    assert r.status_code == 200, r.text


@pytest.fixture()
def rule_on(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="review", title="Sabotage required", summary="s", enforcement="enforced",
        enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=True, default_params={"min_tests_failed": 1}))


def test_a_refused_signoff_that_later_passes_is_held_time(client, auth, key, proj, db, rule_on):
    """The rule refuses a sign-off, the reviewer comes back with a sabotage and it lands: the
    time between is recorded as held by that rule, and served as measured once there are
    enough holds."""
    for i in range(M.MIN_COST_N):
        item, reviewer = _ready_for_review(client, key, effort=0)
        _refused_signoff(client, key, item, reviewer)
        _refused_signoff(client, key, item, reviewer)  # still the one hold
        assert _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"],
                                             "evidence": [SABOTAGE]})["status"] == "done"
        held = db.query(Observation).filter_by(kind=M.HELD_KIND, item_id=item).all()
        assert len(held) == 1 and held[0].rule_id == RULE and held[0].ref["seconds"] >= 0
        if i == 0:
            h = _effect_cost(client, auth, proj)["held_seconds"]
            assert h["status"] == "insufficient" and h["n"] == 1 and h["value"] is None
    h = _effect_cost(client, auth, proj)["held_seconds"]
    assert h["status"] == "measured" and h["n"] == M.MIN_COST_N and h["value"] >= 0
    assert M.open_hold(db, item, RULE) is None


def test_a_signoff_with_no_refusal_records_no_hold(client, key, db, rule_on):
    item, reviewer = _ready_for_review(client, key, effort=0)
    _ok(client, key, "sign_off",
        {"id": item, "agent_id": reviewer["agent_id"], "evidence": [SABOTAGE]})
    assert db.query(Observation).filter_by(kind=M.HELD_KIND).count() == 0


def test_a_person_switching_the_rule_off_overrides_what_it_holds(client, auth, key, proj, db,
                                                                 rule_on):
    """Refused sign-offs, then a person turns the rule off: one override per held item, naming
    the rule, the item and that person, and the hold ends then. Measured against the refusals."""
    item, reviewer = _ready_for_review(client, key, effort=0)
    for _ in range(M.MIN_COST_N):
        _refused_signoff(client, key, item, reviewer)
    o = _effect_cost(client, auth, proj)["overridden"]
    assert o["status"] == "measured" and o["value"] == 0 and o["n"] == M.MIN_COST_N

    _switch_off(client, auth, proj)

    me = client.get("/api/auth/me", headers=auth).json()["id"]
    rows = db.query(Observation).filter_by(kind=M.OVERRIDE_KIND).all()
    assert [(r.rule_id, r.item_id, r.ref["actor"]) for r in rows] == [(RULE, item, me)]
    assert db.query(Observation).filter_by(kind=M.HELD_KIND, item_id=item).count() == 1
    o = _effect_cost(client, auth, proj)["overridden"]
    assert o["status"] == "measured" and o["value"] == 1 and o["n"] == M.MIN_COST_N
    # The sign-off now lands with the rule off; the hold already closed, so nothing doubles.
    _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})
    assert db.query(Observation).filter_by(kind=M.HELD_KIND, item_id=item).count() == 1


def test_switching_off_with_one_refusal_is_insufficient_with_n(client, auth, key, proj, db,
                                                               rule_on):
    item, reviewer = _ready_for_review(client, key, effort=0)
    _refused_signoff(client, key, item, reviewer)
    _switch_off(client, auth, proj)
    o = _effect_cost(client, auth, proj)["overridden"]
    assert o["status"] == "insufficient" and o["n"] == 1 and o["value"] is None
    assert db.query(Observation).filter_by(kind=M.OVERRIDE_KIND, item_id=item).count() == 1


def test_switching_off_with_nothing_held_records_no_override(client, auth, key, proj, db,
                                                             rule_on):
    item, reviewer = _ready_for_review(client, key, effort=0)
    _refused_signoff(client, key, item, reviewer)
    _ok(client, key, "sign_off",
        {"id": item, "agent_id": reviewer["agent_id"], "evidence": [SABOTAGE]})
    _switch_off(client, auth, proj)
    assert db.query(Observation).filter_by(kind=M.OVERRIDE_KIND).count() == 0
