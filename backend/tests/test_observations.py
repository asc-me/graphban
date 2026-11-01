"""The observations ledger (PRD-63 S1 / GRPH-1261): typed facts, never prose."""
import pytest

from app.db import SessionLocal
from app.models import Event, Item, Observation
from app.services import events as events_svc
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import observations as obs
from app.services import rules_recommend as rr

PID = "core"
SHA = "a" * 40
OTHER = "b" * 40


@pytest.fixture()
def db(client):
    s = SessionLocal()
    yield s
    s.close()


def _item(db, *, effort=0, evidence=None, status="done", reviewed_by="rev-1", touchpoints=None):
    it = items_svc.create_item(db, title="obs probe", effort=effort, project_id=PID,
                               touchpoints=touchpoints or [])
    it.status = status
    it.reviewed_by = reviewed_by
    it.evidence = evidence or []
    db.commit()
    return it


def _signoff(commit=SHA, adv=True):
    preds = [{"name": "independent_review", "passed": True, "compared": True, "detail": "d"}]
    if adv:
        preds.append({"name": "adversarial_evidence", "passed": True, "detail": "d"})
    return {"kind": "attestation", "adapter": "fleet.sign_off", "commit": commit,
            "predicates": preds, "detail": "x"}


def _ci(commit, passed):
    return {"kind": "attestation", "adapter": "ci", "commit": commit, "detail": "x",
            "predicates": [{"name": "suite_green", "passed": passed, "compared": True,
                            "detail": "d"}]}


def _kinds(db, item=None):
    q = db.query(Observation)
    if item is not None:
        q = q.filter(Observation.item_id == item.id)
    return sorted(o.kind for o in q)


# ---- derivation ----------------------------------------------------------------------------

def test_exemption_recorded_below_threshold_and_not_above(db):
    low = _item(db, effort=0, evidence=[_signoff()])
    high = _item(db, effort=5, evidence=[_signoff()])
    obs.on_sign_off(db, low, [])
    obs.on_sign_off(db, high, [])
    assert _kinds(db, low) == ["predicate_exempted"]
    assert "predicate_exempted" not in _kinds(db, high)
    row = db.query(Observation).filter_by(item_id=low.id).one()
    assert row.stage == "review" and row.commit == SHA
    assert row.ref == {"predicate": "adversarial_evidence", "effort": 0, "threshold": 3}


def test_ci_red_only_on_a_signed_off_head(db):
    on_head = _item(db, evidence=[_signoff(), _ci(SHA, False)])
    other_head = _item(db, evidence=[_signoff(), _ci(OTHER, False)])
    green = _item(db, evidence=[_signoff(), _ci(SHA, True)])
    never_signed = _item(db, evidence=[_ci(SHA, False)])
    for it in (on_head, other_head, green, never_signed):
        obs.on_evidence(db, it)
    assert _kinds(db, on_head) == ["ci_red_after_sign_off"]
    assert _kinds(db, other_head) == _kinds(db, green) == _kinds(db, never_signed) == []
    assert db.query(Observation).filter_by(item_id=on_head.id).one().stage == "merge"


def test_gap_needs_the_structured_flag_not_the_words(db):
    flagged = _item(db, evidence=[{"kind": "note", "detail": "wiring left", "done": False}])
    prose = _item(db, evidence=[{"kind": "note", "detail": "supervisor wiring not built"}])
    obs.on_sign_off(db, flagged, [])
    obs.on_sign_off(db, prose, [])
    assert "gap_noted_at_sign_off" in _kinds(db, flagged)
    assert "gap_noted_at_sign_off" not in _kinds(db, prose)
    assert db.query(Observation).filter_by(item_id=flagged.id, kind="gap_noted_at_sign_off"
                                           ).one().ref == {"notes": 1}


def test_normalize_keeps_done_false_on_notes_only():
    rows = items_svc.normalize_evidence([
        {"kind": "note", "detail": "a", "done": False}, {"kind": "note", "detail": "b", "done": "no"},
        {"kind": "test", "detail": "c", "done": False}])
    assert rows[0]["done"] is False and "done" not in rows[1] and "done" not in rows[2]


def test_engine_evidence_missing_on_a_migration_item(db):
    mig = ["backend/alembic/versions/0999_x.py"]
    absent = _item(db, touchpoints=mig, evidence=[{"kind": "test", "detail": "sqlite: 10 passed"}])
    denied = _item(db, touchpoints=mig, evidence=[{"kind": "test", "detail": "Postgres NOT run"}])
    ran = _item(db, touchpoints=mig, evidence=[{"kind": "test", "detail": "Postgres: 10 passed"}])
    plain = _item(db, evidence=[])
    for it in (absent, denied, ran, plain):
        obs.on_sign_off(db, it, [])
    assert _kinds(db, absent) == ["evidence_missing"] == _kinds(db, denied)
    assert _kinds(db, ran) == _kinds(db, plain) == []
    assert db.query(Observation).filter_by(item_id=absent.id).one().ref == {"check": "postgres"}


def test_reviewer_sabotage_caught_and_missed(db):
    it = _item(db)
    obs.on_sign_off(db, it, [
        {"kind": "sabotage", "claim": "c1", "mutation": "m", "tests_failed": 2},
        {"kind": "sabotage", "claim": "c2", "mutation": "m", "tests_failed": 0},
        {"kind": "note", "detail": "ignored"}])
    assert _kinds(db, it) == ["reviewer_sabotage_caught", "reviewer_sabotage_missed"]


def test_no_observation_stores_text(db):
    it = _item(db, evidence=[_signoff(), _ci(SHA, False),
                             {"kind": "note", "detail": "SECRET PROSE", "done": False}])
    obs.on_sign_off(db, it, [{"kind": "sabotage", "claim": "SECRET CLAIM", "mutation": "SECRET MUT",
                              "tests_failed": 1}])
    obs.on_evidence(db, it)
    dump = repr([(o.ref, o.dedupe_key) for o in db.query(Observation)])
    assert "SECRET" not in dump and db.query(Observation).count() >= 4


# ---- bounce, reopen, refusal ---------------------------------------------------------------

def test_a_bounce_scores_only_through_a_structured_fact(db):
    text_only = _item(db, status="review", reviewed_by=None, evidence=[])
    failing = _item(db, status="review", reviewed_by=None, evidence=[_ci(SHA, False)])
    fleet_svc.bounce(db, item_id=text_only.id, agent_id="r", reason="this blames the wrong stage")
    fleet_svc.bounce(db, item_id=failing.id, agent_id="r", reason="ci red")
    assert _kinds(db, text_only) == []
    assert _kinds(db, failing) == ["bounce"]
    assert db.query(Observation).filter_by(item_id=failing.id).one().ref == {
        "predicate_failed": ["suite_green"]}


def test_reopen_after_done_counts_each_time(db):
    it = _item(db, reviewed_by=None)
    for _ in range(2):
        items_svc.update_item(db, it.id, status="in_progress")
        it.status = "done"  # re-finished by hand: the completion gate is not under test here
        db.commit()
    items_svc.update_item(db, it.id, status="in_progress")
    assert _kinds(db, it) == ["reopened_after_done"] * 3


def test_a_refusal_with_a_rule_is_recorded_and_an_unmapped_one_is_not(db):
    events_svc.record_refusal(db, rule_id="review.adversarial_evidence", surface="mcp",
                              where="sign_off", status=409, detail="d", target_type="item",
                              target_id=_item(db).id, project_id=PID)
    events_svc.record_refusal(db, rule_id=None, surface="mcp", where="x", status=409, detail="d",
                              project_id=PID)
    rows = db.query(Observation).all()
    assert [(o.kind, o.rule_id, o.stage) for o in rows] == [
        ("refusal", "review.adversarial_evidence", "review")]
    assert rows[0].item_id is not None


# ---- backfill ------------------------------------------------------------------------------

def test_backfill_derives_from_history_and_is_idempotent(db):
    low = _item(db, evidence=[_signoff(), _ci(SHA, False),
                              {"kind": "note", "detail": "n", "done": False}])
    db.add(Event(actor_type="system", surface="mcp", action="refusal", target_type="item",
                 target_id=low.id, project_id=PID,
                 meta={"rule_id": "merge.x", "where": "w", "status": 409}))
    db.commit()
    first = obs.backfill(db, PID)
    assert {"predicate_exempted", "ci_red_after_sign_off", "gap_noted_at_sign_off",
            "refusal"} <= set(first["added"])
    assert obs.backfill(db, PID)["total_added"] == 0


def test_backfill_does_not_double_what_the_live_hook_wrote(db):
    it = _item(db, evidence=[_signoff(), _ci(SHA, False)])
    obs.on_sign_off(db, it, [])
    obs.on_evidence(db, it)
    before = db.query(Observation).count()
    assert before == 2
    assert obs.backfill(db, PID)["total_added"] == 0
    assert db.query(Observation).count() == before


def test_a_failing_write_never_raises_into_the_caller(db, monkeypatch):
    it = _item(db, evidence=[_signoff()])
    monkeypatch.setattr(obs, "_exempted", lambda i: (_ for _ in ()).throw(RuntimeError("boom")))
    obs.on_sign_off(db, it, [])  # must not raise
    assert _kinds(db, it) == []


# ---- read API ------------------------------------------------------------------------------

def test_read_api_filters_and_counts(client, auth, db):
    it = _item(db, evidence=[_signoff(), _ci(SHA, False)])
    obs.on_sign_off(db, it, [])
    obs.on_evidence(db, it)
    r = client.get(f"/api/rules/observations?project_id={PID}", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["counts"] == {"predicate_exempted": 1,
                                                     "ci_red_after_sign_off": 1}
    only = client.get(f"/api/rules/observations?project_id={PID}&kind=ci_red_after_sign_off",
                      headers=auth).json()
    assert only["total"] == 1 and only["observations"][0]["stage"] == "merge"
    staged = client.get(f"/api/rules/observations?project_id={PID}&stage=review",
                        headers=auth).json()
    assert staged["total"] == 1
    assert client.get(f"/api/rules/observations?project_id={PID}&kind=nope",
                      headers=auth).status_code == 422


def test_org_read_spans_the_orgs_projects(client, auth, db):
    from app.models import Organization, OrgMembership, Project, User
    me = db.query(User).filter_by(email="alex@ascme-labs.com").one()
    org = Organization(id="org_obs", name="obs-org")
    db.add(org)
    db.flush()
    db.add(OrgMembership(org_id=org.id, user_id=me.id, role="owner"))
    db.get(Project, PID).org_id = org.id
    db.commit()
    it = _item(db, evidence=[_signoff()])
    obs.on_sign_off(db, it, [])
    r = client.get(f"/api/orgs/{org.id}/rules/observations", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    # a project outside the org contributes nothing
    db.get(Project, PID).org_id = None
    db.commit()
    assert client.get(f"/api/orgs/{org.id}/rules/observations", headers=auth).json()["total"] == 0


def test_backfill_endpoint(client, auth, db):
    _item(db, evidence=[_signoff()])
    r = client.post(f"/api/rules/observations/backfill?project_id={PID}", headers=auth)
    assert r.status_code == 200 and r.json()["added"] == {"predicate_exempted": 1}


# ---- the Recommended panel's `fleet` source ------------------------------------------------

def test_fleet_source_is_listed_and_does_not_read_empty_as_clean(client, auth, db):
    rr.clear_cache()
    src = lambda: next(s for s in client.get(f"/api/rules/recommended?project_id={PID}",
                                             headers=auth).json()["sources"]
                       if s["source"] == "fleet")
    assert src()["state"] == "not_available" and src()["reason"] == "no_observations"
    obs.on_sign_off(db, _item(db, evidence=[_signoff()]), [])
    rr.clear_cache()
    assert src()["state"] == "read" and src()["reason"] == "1 observation(s)"


def test_update_item_evidence_reaches_the_ledger(db):
    """The CALL: the hook in `update_item`, not only `on_evidence` itself."""
    it = _item(db, evidence=[_signoff()])
    items_svc.update_item(db, it.id, evidence=[_ci(SHA, False)])
    assert _kinds(db, it) == ["ci_red_after_sign_off"]


def test_same_fact_on_two_items_is_two_observations(db):
    a = _item(db, evidence=[_signoff()])
    b = _item(db, evidence=[_signoff()])
    obs.on_sign_off(db, a, [{"kind": "sabotage", "claim": "c", "mutation": "m", "tests_failed": 1}])
    obs.on_sign_off(db, b, [{"kind": "sabotage", "claim": "c", "mutation": "m", "tests_failed": 1}])
    assert db.query(Observation).filter_by(kind="reviewer_sabotage_caught").count() == 2
    assert db.query(Observation).filter_by(kind="predicate_exempted").count() == 2
