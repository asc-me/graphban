"""GRPH-1418: a pack installed by one uvicorn worker is in force on every worker.

A second worker is simulated by resetting this process's module state (`LIBRARY`, `CURRENT`,
`CURRENT_SHA`) to what that worker held before the install. The sync runs once at the start
of a request (`app.db.get_db`), before any gate's `RULE not in LIBRARY` guard reads it — so
the gate tests below drive a real `sign_off` over HTTP on the stale worker."""
import pytest

from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_fleet_adversarial_gate import (  # noqa: F401  (fixtures + helpers)
    _ok, _ready_for_review, _rpc, key, proj)
from tests.test_rule_pack import FIX, TRUST, _person, build

SABOTAGE_RULE = "review.sabotage_required"
_SABOTAGE_TOML = (FIX / "api1" / "templates" / "sabotage.toml").read_text()
#: api1 with the sabotage rule firing at any effort, so an effort-0 item exercises it.
ADDS_SABOTAGE = {"templates/sabotage.toml": _SABOTAGE_TOML.replace("min_effort = 3",
                                                                   "min_effort = 0")}
#: api1 with the sabotage rule dropped (its template now carries a different id).
DROPS_SABOTAGE = {"templates/sabotage.toml": _SABOTAGE_TOML.replace(
    'id = "review.sabotage_required"', 'id = "review.sabotage_retired"')}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(P, "TRUSTED_PUBLIC_KEYS", TRUST)
    monkeypatch.setattr(T, "SYNC_TTL", 0.0)
    saved = (dict(T.LIBRARY), P.CURRENT, P.CURRENT_SHA, P.LOAD_REFUSAL, P._REFUSED_SHA)
    T.LIBRARY.clear()
    P.CURRENT, P.CURRENT_SHA, P.LOAD_REFUSAL, P._REFUSED_SHA = None, "", "", ""
    yield
    T.LIBRARY.clear()
    T.LIBRARY.update(saved[0])
    P.CURRENT, P.CURRENT_SHA, P.LOAD_REFUSAL, P._REFUSED_SHA = saved[1:]


def _stale_worker(rule_ids, sha="old-sha"):
    """This process as another worker left it: an older library, an older sha. Its rules are
    on by default and the sabotage one fires at any effort."""
    T.LIBRARY.clear()
    for rid in rule_ids:
        T.register(T.LibraryRule(
            id=rid, stage="review", title=rid, summary="", enforcement="enforced",
            enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
            default_on=True, default_params={"min_effort": 0}))
    P.CURRENT_SHA = sha


def _install_by_other_worker(edits=None):
    db, user = _person()
    try:
        art, sig = build(FIX / "api1", edits)
        P.accept(db, user, art, sig, P.sha256_of(art), trusted_keys=TRUST)
        return P.sha256_of(art), set(T.LIBRARY)
    finally:
        db.close()


def test_stale_worker_sees_new_pack_rules_at_request_start(client):
    sha, installed_rules = _install_by_other_worker()
    _stale_worker({"old.only_rule"})
    assert "review.evidence_attached" not in T.LIBRARY
    db, _ = _person()
    try:
        T.sync_library(db)  # what get_db does before the request reads anything
        ids = {t["rule_id"] for t in T.effective(db, None)}
    finally:
        db.close()
    assert ids == installed_rules and "old.only_rule" not in ids
    assert P.CURRENT_SHA == sha


def test_a_rule_another_worker_installed_is_enforced_on_a_stale_worker(client, key):
    item, reviewer = _ready_for_review(client, key, effort=0)
    _install_by_other_worker(ADDS_SABOTAGE)
    _stale_worker({"old.only_rule"})
    assert SABOTAGE_RULE not in T.LIBRARY

    res = _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict" and SABOTAGE_RULE in err["message"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_a_rule_the_new_pack_dropped_stops_being_enforced_on_a_stale_worker(client, key):
    item, reviewer = _ready_for_review(client, key, effort=0)
    _install_by_other_worker(DROPS_SABOTAGE)
    _stale_worker({SABOTAGE_RULE})

    out = _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    assert out["status"] == "done"  # neither refused by the old rule nor a 404 on it
    assert SABOTAGE_RULE not in T.LIBRARY


def test_a_dropped_rule_stops_being_offered_on_a_stale_worker(client, key):
    _install_by_other_worker(DROPS_SABOTAGE)
    _stale_worker({SABOTAGE_RULE})

    rows = _ok(client, key, "get_rule_toggles", {})

    assert SABOTAGE_RULE not in str(rows)
    assert "review.sabotage_retired" in str(rows)


def test_check_is_throttled(client, monkeypatch):
    _install_by_other_worker()
    _stale_worker({"old.only_rule"})
    monkeypatch.setattr(T, "SYNC_TTL", 3600.0)
    db, _ = _person()
    try:
        T.sync_library(db)  # opens the window
        _stale_worker({"old.only_rule"})
        T.sync_library(db)
        assert "old.only_rule" in T.LIBRARY  # inside the window: no re-read
    finally:
        db.close()
