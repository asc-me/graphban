"""GRPH-1418: a pack installed by one uvicorn worker is in force on every worker.

A second worker is simulated by resetting this process's module state (`LIBRARY`, `CURRENT`,
`CURRENT_SHA`) to what that worker held before the install, then reading through the toggle
service the way a request would."""
import pytest

from app.services import rule_pack as P
from app.services import rule_toggles as T
from tests.test_rule_pack import FIX, TRUST, _person, build


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


def _stale_worker(rule_ids, sha):
    """This process as another worker left it: an older library, an older sha."""
    T.LIBRARY.clear()
    for rid in rule_ids:
        T.register(T.LibraryRule(id=rid, stage="build", title=rid,
                                 summary="", enforcement="checked", enforcer=None))
    P.CURRENT_SHA = sha


def _install_by_other_worker(db, user):
    art, sig = build(FIX / "api1")
    P.accept(db, user, art, sig, P.sha256_of(art), trusted_keys=TRUST)
    return P.sha256_of(art), set(T.LIBRARY)


def test_stale_worker_sees_new_pack_rules_without_restart(client):
    db, user = _person()
    try:
        sha, installed_rules = _install_by_other_worker(db, user)
        _stale_worker({"old.only_rule"}, "old-sha")
        assert "review.evidence_attached" not in T.LIBRARY
        ids = {t["rule_id"] for t in T.effective(db, None)}
        assert ids == installed_rules and "old.only_rule" not in ids
        assert P.CURRENT_SHA == sha
    finally:
        db.close()


def test_replaced_rule_stops_being_offered_on_a_stale_worker(client):
    db, user = _person()
    try:
        _install_by_other_worker(db, user)
        _stale_worker({"old.only_rule"}, "old-sha")
        with pytest.raises(Exception):
            T.resolve(db, "old.only_rule", None)
    finally:
        db.close()


def test_check_is_throttled(client, monkeypatch):
    db, user = _person()
    try:
        _install_by_other_worker(db, user)
        _stale_worker({"old.only_rule"}, "old-sha")
        monkeypatch.setattr(T, "SYNC_TTL", 3600.0)
        T._sync_library(db)  # opens the window
        _stale_worker({"old.only_rule"}, "old-sha")
        T.effective(db, None)
        assert "old.only_rule" in T.LIBRARY  # inside the window: no re-read
    finally:
        db.close()
