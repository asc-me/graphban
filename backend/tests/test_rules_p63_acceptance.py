"""PRD-63 §12 acceptance walk (GRPH-1266): the GRPH-P62 replay, criteria 1-2, against the pinned
pack fixture (criterion 11: no rules repo). Criteria 3-10 are walked by the suites named in
`test_every_criterion_has_a_named_suite`, which fails if one of them goes missing."""
from pathlib import Path

import pytest

from app.db import SessionLocal
from app.models import Observation, User
from app.services import observations as obs
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules_recommend as rr
from tests.test_observations import _ci, _item, _signoff
from tests.test_rule_pack import FIX, TRUST, build

PID = "core"
GAP_ITEM = "GRPH-1249"
CI_ITEM = "GRPH-1255"


@pytest.fixture()
def db(client):
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _pack(client, db):
    saved = (dict(T.LIBRARY), P.CURRENT)
    # the fixture's sabotage and reopen rules default on; the replay asks what to turn ON
    edits = {f"templates/{n}.toml": (FIX / f"api1/templates/{n}.toml").read_text().replace(
        'default = "on"', 'default = "off"') for n in ("sabotage", "reopen_on_red")}
    # the fixture has no gap template (test_rule_gap_blocks_sign_off swaps one in the same way)
    edits["templates/evidence.toml"] = (
        'id = "review.gap_blocks_sign_off"\nstage = "Review"\ntitle = "Gaps need a follow-up"\n'
        'summary = "Every not-done note links a follow-up item."\n'
        'primitive = "gap_followup_link"\nclaims = "enforced"\ndefault = "off"\n')
    P.install(db, db.query(User).first(), *build(FIX / "api1", edits), trusted_keys=TRUST)
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
    for _ in range(1):
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


CRITERIA = {
    3: ("test_rule_sabotage_required.py", "test_on_refuses_an_effort_zero_item_without_a_sabotage"),
    4: ("test_rule_toggles.py", "test_the_recommendation_engine_cannot_set_a_toggle"),
    5: ("test_rule_toggles.py", "test_a_locked_rule_refuses_a_project_override_with_its_rule_id"),
    6: ("test_rule_measure.py", "test_escape_rate_insufficient_not_a_number"),
    7: ("test_rule_measure.py", "test_platform_top_org_share_cap"),
    8: ("test_rule_toggles.py", "test_org_writes_need_team_or_enterprise"),
    9: ("test_rule_pack.py", "test_newer_server_required_names_both_versions_and_last_good_stays"),
    10: ("test_rules_org.py", "test_drift_clean_then_weakening_when_a_locked_pin_is_turned_off"),
}


@pytest.mark.parametrize("n", sorted(CRITERIA))
def test_every_criterion_has_a_named_suite(n):
    f, name = CRITERIA[n]
    assert f"def {name}(" in (Path(__file__).parent / f).read_text()
