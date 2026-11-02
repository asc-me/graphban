"""PRD-63 §12 acceptance walk (GRPH-1266), against the pinned pack fixture (criterion 11: no
rules repo). Criteria 1-4, 6-8 and 10 are walked here on the installed pack, and so is the part
of 5 that says "every project". Where a sub-clause is already exercised by its slice's own
suite, `CLAUSES` names that test, one entry per clause; `test_every_clause_names_a_test` fails
if one of them goes missing."""
from pathlib import Path

import pytest

from app.config import settings
from app.db import SessionLocal
from app.errors import Refused
from app.models import Event, Observation, PlatformObservationRollup, RuleToggle, User
from app.services import fleet
from app.services import observations as obs
from app.services import rule_measure as M
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules_org as RO
from app.services import rules_recommend as rr
from tests.test_fleet_adversarial_gate import _rpc, _ready_for_review, key, proj  # noqa: F401
from tests.test_observations import _ci, _item, _signoff
from tests.test_rule_measure import _adopt, _kind_obs, _org, _signoffs
from tests.test_rule_pack import FIX, TRUST, build
from tests.test_rule_toggles import _login, _set_plan

PID = "core"
GAP_ITEM = "GRPH-1249"
CI_ITEM = "GRPH-1255"


@pytest.fixture()
def db(client):
    s = SessionLocal()
    yield s
    s.close()


def _edits() -> dict[str, str]:
    # the fixture's sabotage and reopen rules default on; the replay asks what to turn ON
    edits = {f"templates/{n}.toml": (FIX / f"api1/templates/{n}.toml").read_text().replace(
        'default = "on"', 'default = "off"') for n in ("sabotage", "reopen_on_red")}
    # the fixture has no gap template (test_rule_gap_blocks_sign_off swaps one in the same way)
    edits["templates/evidence.toml"] = (
        'id = "review.gap_blocks_sign_off"\nstage = "Review"\ntitle = "Gaps need a follow-up"\n'
        'summary = "Every not-done note links a follow-up item."\n'
        'primitive = "gap_followup_link"\nclaims = "enforced"\ndefault = "off"\n')
    return edits


@pytest.fixture(autouse=True)
def _pack(client, db):
    saved = (dict(T.LIBRARY), P.CURRENT)
    P.install(db, db.query(User).first(), *build(FIX / "api1", _edits()), trusted_keys=TRUST)
    rr.clear_cache()
    yield
    T.LIBRARY.clear()
    T.LIBRARY.update(saved[0])
    P.CURRENT = saved[1]
    rr.clear_cache()


def _replay_p62(db):
    """The P62 wave in miniature: effort-0 slices signed off by exemption, one signed-off head
    that went red, one structured gap, and migration slices whose builders ran no Postgres."""
    slices = [_item(db, effort=0, evidence=[_signoff()]) for _ in range(3)]
    red = _item(db, evidence=[_signoff(), _ci("a" * 40, False)])
    gap = _item(db, evidence=[{"kind": "note", "detail": "wiring left", "done": False}])
    pg = [_item(db, status="review", reviewed_by=None, touchpoints=["alembic/versions/0171_x.py"],
                evidence=[{"kind": "test", "detail": "Postgres NOT run"}]) for _ in range(2)]
    return slices, red, gap, pg


def test_criterion_1_replay_produces_the_four_observation_kinds(db):
    slices, red, gap, pg = _replay_p62(db)
    out = obs.backfill(db, PID)["added"]
    assert out["predicate_exempted"] >= 3 and out["evidence_missing"] == 2
    assert out["ci_red_after_sign_off"] == 1 and out["gap_noted_at_sign_off"] == 1
    rows = db.query(Observation)
    assert {s.id for s in slices} <= {o.item_id for o in rows.filter_by(kind="predicate_exempted")}
    assert rows.filter_by(kind="ci_red_after_sign_off").one().item_id == red.id
    assert rows.filter_by(kind="gap_noted_at_sign_off").one().item_id == gap.id
    assert {o.item_id for o in rows.filter_by(kind="evidence_missing")} == {i.id for i in pg}
    assert all(o.ref == {"check": "postgres"} for o in rows.filter_by(kind="evidence_missing"))
    assert obs.backfill(db, PID)["total_added"] == 0  # a replay twice adds nothing


def test_criterion_2_recommended_suggests_the_four_rules_each_linking_observations(
        client, auth, db):
    _replay_p62(db)
    # a second red head and a second gap: one observation of a kind is not a pattern
    _item(db, evidence=[_signoff("c" * 40), _ci("c" * 40, False)])
    _item(db, evidence=[{"kind": "note", "detail": "x", "done": False}])
    obs.backfill(db, PID)
    rr.clear_cache()
    body = client.get(f"/api/rules/recommended?project_id={PID}", headers=auth).json()
    sug = {s["rule_id"]: s for s in body["suggestions"] if s["kind"] == "rule"}
    assert set(sug) == {"review.sabotage_required", "merge.reopen_on_red",
                        "review.gap_blocks_sign_off", "build.engine_evidence"}
    for s in sug.values():
        links = [e["link"] for e in s["evidence"] if e["link"]]
        assert links and s["score"] > 0
        ids = {o.item_id for o in db.query(Observation).filter(Observation.item_id.in_(links))}
        assert ids == set(links)  # every link is an item that carries an observation


def test_recommending_turns_nothing_on(client, auth, db):
    _replay_p62(db)
    obs.backfill(db, PID)
    before = {r: T.resolve(db, r, PID) for r in T.LIBRARY}
    client.get(f"/api/rules/recommended?project_id={PID}", headers=auth)
    assert {r: T.resolve(db, r, PID) for r in T.LIBRARY} == before


def test_criterion_11_pack_comes_from_the_pinned_fixture_not_a_rules_repo():
    assert P.CURRENT is not None and P.CURRENT.name == "graphban-rules-fixture"
    assert (Path(FIX) / "TEST_ONLY_signing_seed.hex").is_file()


# ---- 3: turning review.sabotage_required on and off, on the installed pack -----------------

SAB = "review.sabotage_required"


def _walk_3(client, auth, key, proj):
    def put(body):
        r = client.put(f"/api/rules/toggles/{SAB}", params={"project_id": proj}, json=body,
                       headers=auth)
        assert r.status_code == 200, r.text

    def drawn():
        r = client.get("/api/rules/flow", params={"project_id": proj}, headers=auth)
        return next(x for st in r.json()["stages"] for x in st["rules"] if x["id"] == SAB)

    assert drawn()["used"] is False
    put({"on": True, "params": {"min_effort": 0}, "reason": "P62: effort-0 exemptions"})
    entry = next(x for x in client.get("/api/rules", params={"project_id": proj},
                                       headers=auth).json()["rules"] if x["id"] == SAB)
    # Drawn at the level its primitive provides, read from the primitive, not the template.
    assert entry["enforcement"] == P.PRIMITIVES["sabotage_receipt"].level == "enforced"
    assert entry["control"] == "toggle" and drawn()["used"] is True

    item, reviewer = _ready_for_review(client, key, effort=0)
    res = _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})
    assert res.get("isError") and SAB in res["structuredContent"]["error"]["message"]

    put({"on": False, "reason": "dropped"})
    assert drawn()["used"] is False


def test_criterion_3_on_refuses_without_a_sabotage_and_off_draws_not_used(client, auth, key,
                                                                         proj):
    _walk_3(client, auth, key, proj)


def test_criterion_3_a_sabotage_that_disables_the_primitive_fails_the_walk(
        client, auth, key, proj, monkeypatch):
    monkeypatch.setattr(fleet, "require_reviewer_sabotage", lambda *a, **k: None)
    with pytest.raises(AssertionError):
        _walk_3(client, auth, key, proj)


# ---- 5, 8, 10: an org on the installed pack with a Regulated profile ----------------------

#: §6.1's Regulated column: every seed rule on.
REGULATED = {"review.sabotage_required": {"min_effort": 0}, "review.distinct_vendor": None,
             "review.gap_blocks_sign_off": None, "build.engine_evidence": None,
             "merge.reopen_on_red": None, "review.staff_every_row": None,
             "accept.human_clause_blocks": None}


def _regulated_toml(min_effort=0, version="1"):
    lines = ['id = "regulated"', 'title = "Regulated"',
             'summary = "Every AI-built change independently checked."', f'version = "{version}"',
             "[rules]"]
    for rid in REGULATED:
        params = f", params = {{ min_effort = {min_effort} }}" if rid == SAB else ""
        lines.append(f'"{rid}" = {{ state = "on"{params} }}')
    return "\n".join(lines) + "\n"


def _regulated_pack(min_effort=0, pack_version="1.0.0", profile_version="1"):
    edits = {**_edits(), "profiles/standard.toml": _regulated_toml(min_effort, profile_version)}
    if pack_version != "1.0.0":
        edits["pack.toml"] = (FIX / "api1/pack.toml").read_text().replace(
            'version = "1.0.0"', f'version = "{pack_version}"')
    return build(FIX / "api1", edits)


@pytest.fixture()
def org(client, db, monkeypatch):
    monkeypatch.setattr(settings, "hosted_mode", True)
    P.install(db, db.query(User).first(), *_regulated_pack(), trusted_keys=TRUST)
    RO.clear_cache()
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Regulated Co"}, headers=owner).json()
    ps = [client.post("/api/projects", json={"name": f"R{i}"}, headers=owner).json()["id"]
          for i in range(2)]
    _set_plan(o["id"], "team")
    return {"owner": owner, "id": o["id"], "ps": ps}


def _apply(client, org):
    return client.post(f"/api/orgs/{org['id']}/rules/profiles/regulated/apply",
                       json={"reason": "we ship to a regulator"}, headers=org["owner"])


def _toggles(client, org, pid):
    r = client.get("/api/rules/toggles", params={"project_id": pid}, headers=org["owner"])
    return {t["rule_id"]: t for t in r.json()["toggles"]}


def _drift(client, org):
    return client.get(f"/api/orgs/{org['id']}/rules/profiles/regulated/drift",
                      headers=org["owner"]).json()


def test_criterion_10_regulated_sets_each_rule_as_listed_with_one_profile_event(client, org):
    r = _apply(client, org)
    assert r.status_code == 200, r.text
    for pid in org["ps"]:
        t = _toggles(client, org, pid)
        for rid, params in REGULATED.items():
            assert t[rid]["on"] == {"value": True, "source": "org"}, (pid, rid)
            if params:
                assert t[rid]["params"]["value"] == {**T.LIBRARY[rid].default_params, **params}
    s = SessionLocal()
    try:
        ev = s.query(Event).filter(Event.action == "apply_rule_profile").all()
    finally:
        s.close()
    assert len(ev) == 1
    assert ev[0].meta["profile"] == "regulated" and ev[0].meta["profile_version"] == "1"
    assert ev[0].meta["pack_version"] == "1.0.0"
    assert _drift(client, org)["state"] == "clean"


def test_criterion_10_changing_one_rule_differs_from_regulated_in_1_rule(client, org):
    _apply(client, org)
    r = client.put(f"/api/orgs/{org['id']}/rule-toggles/review.distinct_vendor",
                   json={"on": False, "reason": "single-vendor fleet"}, headers=org["owner"])
    assert r.status_code == 200, r.text
    d = _drift(client, org)
    assert d["state"] == "drifted"
    assert {x["rule_id"] for x in d["drift"]} == {"review.distinct_vendor"}  # "in 1 rule"
    assert {x["project_id"] for x in d["drift"]} == set(org["ps"])
    assert all(x["kind"] == "weakening" for x in d["drift"])


def test_criterion_10_a_newer_pack_changing_regulated_is_offered_with_its_diff(client, db, org):
    _apply(client, org)
    d = P.offer(*_regulated_pack(min_effort=2, pack_version="1.1.0", profile_version="2"),
                trusted_keys=TRUST)
    assert d["from"] == "1.0.0" and d["to"] == "1.1.0"
    change = d["profiles"]["changed"]["regulated"]
    assert change["version"] == {"from": "1", "to": "2"}
    assert change["rules"]["from"][SAB]["params"] == {"min_effort": 0}
    assert change["rules"]["to"][SAB]["params"] == {"min_effort": 2}
    # Offered, never applied: the pack, the profile and every project are as they were.
    assert P.CURRENT.version == "1.0.0"
    assert P.CURRENT.profiles["regulated"]["version"] == "1"
    assert _toggles(client, org, org["ps"][0])[SAB]["params"]["value"]["min_effort"] == 0
    assert _drift(client, org)["state"] == "clean"


def test_criterion_5_a_locked_rule_is_read_only_on_every_project_and_refuses_overrides(
        client, org):
    _apply(client, org)
    s = SessionLocal()
    try:
        for pid in org["ps"]:
            t = _toggles(client, org, pid)[SAB]
            assert t["locked"] is True and t["locked_by"] == org["id"]
            assert t["on"]["source"] == "org"
            r = client.put(f"/api/rules/toggles/{SAB}", params={"project_id": pid},
                           json={"on": False, "reason": "too slow"}, headers=org["owner"])
            assert r.status_code == 409, r.text
            ev = s.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
            assert ev.meta["rule_id"] == T.LOCK_RULE and ev.meta["locked_rule"] == SAB
    finally:
        s.close()


@pytest.mark.parametrize("plan", ["free", "pro"])
def test_criterion_8_applying_a_profile_to_an_org_is_402_on_free_and_pro(client, org, plan):
    _set_plan(org["id"], plan)
    r = _apply(client, org)
    assert r.status_code == 402, r.text
    assert "org-level rules" in r.json()["detail"]
    assert _toggles(client, org, org["ps"][0])[SAB]["on"]["source"] == "platform default"


# ---- 4: the engine can set no toggle and apply no profile ---------------------------------

def test_criterion_4_the_engine_applying_a_profile_is_refused(client, db, org):
    with pytest.raises(Refused) as e:
        RO.apply_profile(db, rr.ENGINE_IDENTITY, org["id"], "regulated", reason="suggested")
    assert e.value.rule_id == T.PERSON_RULE
    with pytest.raises(Refused) as e:
        T.set_project_toggle(db, org["ps"][0], SAB, {"on": True},
                             actor=rr.ENGINE_IDENTITY, reason="suggested")
    assert e.value.rule_id == T.PERSON_RULE
    assert db.query(RuleToggle).count() == 0
    assert db.query(Event).filter(Event.action.in_(
        ("set_rule_toggle", "apply_rule_profile"))).count() == 0


# ---- 6: caught with links, escape rates with n, "not enough data" -------------------------

def test_criterion_6_caught_links_its_items_and_thin_data_is_never_a_number(db):
    it = _item(db, effort=1)
    _adopt(db, 10)
    db.add(Observation(project_id=PID, kind="refusal", stage="review", rule_id=SAB,
                       item_id=it.id, dedupe_key="c1"))
    db.commit()
    cell = next(r for r in M.project_effect(db, PID)["rules"] if r["rule_id"] == SAB)
    assert cell["caught"]["value"] == 1 and cell["caught"]["items"] == [it.id]
    for side in ("before", "after"):
        rate = cell["escape_rate"][side]
        assert rate["status"] == "insufficient" and rate["value"] is None
        assert "not enough data" in rate["reason"] and rate["n"] == 0
    _signoffs(db, 30, 15, "b")
    _signoffs(db, 30, 5, "a")
    cell = next(r for r in M.project_effect(db, PID)["rules"] if r["rule_id"] == SAB)
    assert cell["escape_rate"]["before"]["n"] == 30
    assert cell["escape_rate"]["after"]["status"] == "measured"
    assert cell["escape_rate"]["after"]["value"] == 0 and cell["escape_rate"]["after"]["n"] == 30


# ---- 7: the P62 kinds in the platform aggregates (GRPH-1286's observations by kind) --------

P62_KINDS = ("predicate_exempted", "ci_red_after_sign_off", "gap_noted_at_sign_off",
             "evidence_missing")


def test_criterion_7_platform_observations_only_above_floors_with_no_ids_or_text(
        db, monkeypatch):
    monkeypatch.setattr(settings, "hosted_mode", True)
    orgs = [_org(db, f"o{i}p62", True) for i in range(3)]
    out_org, out_pid = _org(db, "outp62", False)
    for _, pid in orgs:
        _kind_obs(db, pid, "predicate_exempted", 10)
        _kind_obs(db, pid, "ci_red_after_sign_off", 1)
    _kind_obs(db, out_pid, "predicate_exempted", 500)  # would dominate if it counted
    M.platform_roll(db)
    db.commit()
    out = M.platform_effect(db, orgs[0][0].id)
    cells = {c["kind"]: c for c in out["observations"]}
    assert set(P62_KINDS) <= set(cells)
    exempt = cells["predicate_exempted"]
    assert exempt["available"] is True and exempt["n"] is None and exempt["n_band"] == "20–49"
    # 3 observations across the platform: under PLATFORM_MIN_N, so no rate is served
    assert cells["ci_red_after_sign_off"]["available"] is False
    assert cells["gap_noted_at_sign_off"]["available"] is False
    # The org that did not opt in contributes nothing, and sees nothing.
    row = db.query(PlatformObservationRollup).filter_by(kind="predicate_exempted").one()
    assert row.observations == 30 and row.orgs_contributing == 3
    assert M.platform_effect(db, out_org.id)["available"] is False
    blob = repr(out)
    for o, pid in orgs + [(out_org, out_pid)]:
        assert o.id not in blob and pid not in blob and o.name not in blob


# ---- clauses walked by their slice's own suite --------------------------------------------

CLAUSES = [
    (3, "on refuses a sign-off with no reviewer sabotage, at any effort",
     "test_rule_sabotage_required.py", "test_on_refuses_an_effort_zero_item_without_a_sabotage"),
    (3, "a sabotage that broke nothing does not satisfy it",
     "test_rule_sabotage_required.py", "test_a_sabotage_that_broke_nothing_does_not_satisfy"),
    (3, "the registry draws it with db truth and a toggle, at its level",
     "test_rule_toggles.py", "test_registry_draws_a_library_rule_with_db_truth_and_a_toggle"),
    (3, "off is drawn not used, never hidden",
     "test_rule_toggles.py", "test_an_off_toggle_is_drawn_not_used_never_hidden"),
    (4, "the engine cannot set a project or org toggle",
     "test_rule_toggles.py", "test_the_recommendation_engine_cannot_set_a_toggle"),
    (4, "an agent key cannot set a toggle over REST or at the service",
     "test_rule_toggles.py", "test_an_agent_key_cannot_set_a_toggle"),
    (4, "no MCP tool writes a toggle", "test_rule_toggles.py", "test_no_mcp_tool_writes_a_toggle"),
    (4, "only a person installs a pack", "test_rule_pack.py", "test_only_a_person_installs_a_pack"),
    (5, "a locked rule refuses a project override with its rule_id",
     "test_rule_toggles.py", "test_a_locked_rule_refuses_a_project_override_with_its_rule_id"),
    (6, "caught counts refusals naming the rule",
     "test_rule_measure.py", "test_caught_counts_refusals_naming_the_rule"),
    (6, "escape rate before and after adoption with n",
     "test_rule_measure.py", "test_escape_rate_before_after_with_n"),
    (6, "too little data is not a number",
     "test_rule_measure.py", "test_escape_rate_insufficient_not_a_number"),
    (7, "platform rule rates only above the floors and without ids",
     "test_rule_measure.py", "test_platform_served_only_above_floors_and_without_ids"),
    (7, "platform rule rates under the top-org-share cap",
     "test_rule_measure.py", "test_platform_top_org_share_cap"),
    (7, "platform observations under the top-org-share cap",
     "test_rule_measure.py", "test_platform_observations_need_three_orgs_and_respect_share_cap"),
    (7, "a non-contributing org contributes nothing",
     "test_rule_measure.py", "test_non_contributing_org_contributes_nothing_and_sees_nothing"),
    (7, "opting out purges observations on the next roll",
     "test_rule_measure.py", "test_opting_out_purges_observations_on_next_roll"),
    (8, "org toggle writes are 402 on free and pro",
     "test_rule_toggles.py", "test_org_writes_need_team_or_enterprise"),
    (8, "org measurement and platform rates are plan-gated",
     "test_rule_measure.py", "test_org_and_platform_are_plan_gated"),
    (9, "a template claiming enforced on a checked primitive is refused at load",
     "test_rule_pack.py", "test_enforced_claim_on_a_checked_primitive_is_refused"),
    (9, "product CI loads the pinned current-major fixture",
     "test_rule_pack.py", "test_current_major_fixture_loads_every_template"),
    (9, "the previous-major fixture is drawn unmeasured, not half-enforced",
     "test_rule_pack.py", "test_previous_major_fixture_is_drawn_unmeasured_not_half_enforced"),
    (9, "a newer-server pack is refused naming both versions; the last good pack stays",
     "test_rule_pack.py", "test_newer_server_required_names_both_versions_and_last_good_stays"),
]


@pytest.mark.parametrize("n,clause,f,name", CLAUSES, ids=[f"{c[0]}:{c[3]}" for c in CLAUSES])
def test_every_clause_names_a_test(n, clause, f, name):
    assert f"def {name}(" in (Path(__file__).parent / f).read_text()
