"""PRD-61 §8 acceptance: the registry's counts are honest, and the guards actually bite."""
import dataclasses

import pytest

from app.services import rules as R
from tests import test_rules_registry as reg


@pytest.fixture
def db():
    from app.db import SessionLocal
    s = SessionLocal()
    yield s
    s.close()


def _flow_rules(db):
    f = R.flow(db, None)
    return [r for s in f["stages"] for r in s["rules"]]


def test_every_registered_rule_is_on_a_stage_and_no_stage_beats_its_weakest(db):
    f = R.flow(db, None)
    placed = {r["id"] for s in f["stages"] for r in s["rules"]}
    assert set(R.RULES_BY_ID) <= placed
    for s in f["stages"]:
        used = [r["enforcement"] for r in s["rules"] if r["used"]]
        if used:
            assert s["state"] == R.weakest(used)


def test_legend_enforced_count_is_the_rules_with_a_server_refusal_path(db):
    enforced = [r for r in _flow_rules(db) if r["enforcement"] == "enforced"]
    with_path = [
        r for r in R.RULES
        if r.enforcer.kind == "graphban_source" and r.enforcer.symbol
        and R.raises_symbol((R.BACKEND_ROOT / r.enforcer.ref).read_text(), r.enforcer.symbol)
        and r.enforcement == "enforced"
    ]
    assert len(enforced) == len(with_path) > 0
    assert not [r for r in enforced if r.get("value") is not None and r["id"].startswith("gitops.")]
    assert not [r for r in R.RULES if r.gitops_field and r.enforcement == "enforced"]


def _with(monkeypatch, rule_id, **changes):
    rules = tuple(dataclasses.replace(r, **changes) if r.id == rule_id else r for r in R.RULES)
    monkeypatch.setattr(R, "RULES", rules)


def _first(pred):
    return next(r for r in R.RULES if pred(r))


def test_sabotage_a_deleted_enforcer_fails_the_registry_test(monkeypatch):
    r = _first(lambda r: r.enforcement == "enforced")
    _with(monkeypatch, r.id, enforcer=dataclasses.replace(r.enforcer, symbol="NoSuchEnforcer"))
    with pytest.raises(AssertionError):
        reg.test_enforced_rules_have_a_refusal_path()


def test_sabotage_a_gitops_rule_marked_enforced_fails(monkeypatch):
    r = _first(lambda r: r.gitops_field)
    _with(monkeypatch, r.id, enforcement="enforced", channel=None)
    with pytest.raises(AssertionError):
        reg.test_gitops_rules_cover_the_view()


def test_sabotage_an_advisory_rule_marked_enforced_without_a_path_fails(monkeypatch):
    r = _first(lambda r: r.enforcement == "advisory" and not r.gitops_field)
    _with(monkeypatch, r.id, enforcement="enforced", channel=None)
    with pytest.raises(AssertionError):
        reg.test_enforced_rules_have_a_refusal_path()
