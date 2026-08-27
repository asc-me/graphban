"""The bounded retry and the loop that must not take the app down (PRD-25 S2b, GRPH-521).

Two claims carry this slice and neither is provable by reading the code.

**The budget decrements exactly once when the loop and a click race.** The conditional update
is what makes that true, and a version without the `WHERE next_attempt_at = ...` clause behaves
identically in every single-caller test — it only diverges under contention, which is precisely
the case a test has to construct deliberately.

**The app serves with the loop running, and with it throwing.** This is the service's first
background task. A loop that raises out of `lifespan` takes down every self-hosted install on
startup, and "we catch exceptions" is a sentence, not evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Credential
from app.security import secrets
from app.services import credential_retry as cr


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _cred(db, cid="cred_1", *, state=cr.PENDING, attempts=0, next_at=None,
          model="claude-x", kind="anthropic"):
    c = Credential(id=cid, kind=kind, model=model, label=cid, state=state,
                   api_key=secrets.encrypt("sk"), validation_attempts=attempts,
                   next_attempt_at=next_at)
    db.add(c)
    db.commit()
    return c


def _answers(monkeypatch, models):
    monkeypatch.setattr(cr.probe, "known_models",
                        lambda *a, **k: None if models is None else frozenset(models))


# ---- the race -----------------------------------------------------------------------------


def test_two_claimants_race_and_the_budget_moves_once(db):
    """THE claim. Both read the same `next_attempt_at`; exactly one wins.

    Without the conditional clause both would increment and a five-attempt budget would be
    spent in two or three real attempts — the row reaching `unreachable` while having actually
    been asked twice.
    """
    cred = _cred(db, "cred_1")

    first = cr.claim(db, cred, _now := datetime.now(timezone.utc))
    db.expire_all()
    stale = db.get(Credential, "cred_1")
    # The second claimant read the row BEFORE the first claim landed — reconstructed here,
    # because that is the only state in which the race is real.
    stale.next_attempt_at = None
    second = cr.claim(db, stale, _now)

    assert first is True
    assert second is False, "both claimants took the attempt — the conditional clause is gone"
    assert db.get(Credential, "cred_1").validation_attempts == 1


def test_the_button_and_the_loop_do_not_double_spend(db, monkeypatch):
    """The operator presses `Test connection` exactly when a row looks stuck, which is when
    the loop is most likely to be picking it up. Both go through `claim`."""
    _answers(monkeypatch, None)
    _cred(db, "cred_1")

    cr.run_once(db)
    before = db.get(Credential, "cred_1").validation_attempts

    cr.retry_now(db, "cred_1", "")
    after = db.get(Credential, "cred_1").validation_attempts

    assert before == 1
    assert after == 2, f"two distinct attempts should cost two, got {after - before}"


def test_a_fresh_row_can_be_claimed_at_all(db):
    """The NULL trap. A never-attempted row has `next_attempt_at IS NULL`, and `NULL = NULL`
    is false — so the ticket's literal `WHERE next_attempt_at = :value_i_read` can never claim
    a fresh row and the FIRST attempt never happens. Silent: the row just sits there."""
    cred = _cred(db, "cred_1", next_at=None)

    assert cr.claim(db, cred) is True
    assert db.get(Credential, "cred_1").validation_attempts == 1


# ---- the budget and its end -----------------------------------------------------------------


def test_the_budget_is_bounded_and_ends_unreachable(db, monkeypatch):
    """`unreachable` exists so a row never claims a retry that will not happen."""
    _answers(monkeypatch, None)
    _cred(db, "cred_1")

    for _ in range(cr.MAX_ATTEMPTS + 2):
        # Force each attempt due rather than waiting out the real backoff.
        row = db.get(Credential, "cred_1")
        row.next_attempt_at = None
        db.commit()
        cr.run_once(db)

    row = db.get(Credential, "cred_1")
    assert row.state == cr.UNREACHABLE
    assert row.validation_attempts == cr.MAX_ATTEMPTS, (
        f"the budget was not bounded: {row.validation_attempts} attempts made"
    )
    assert row.next_attempt_at is None, (
        "an unreachable row is still scheduled — it claims a retry that will not happen"
    )


def test_an_unreachable_row_is_not_picked_up_again(db, monkeypatch):
    _answers(monkeypatch, None)
    _cred(db, "cred_1", state=cr.UNREACHABLE, attempts=cr.MAX_ATTEMPTS)

    assert cr.run_once(db) == 0


def test_a_successful_probe_clears_the_schedule(db, monkeypatch):
    _answers(monkeypatch, {"claude-x"})
    _cred(db, "cred_1")

    cr.run_once(db)

    row = db.get(Credential, "cred_1")
    assert row.state == cr.VALID
    assert row.next_attempt_at is None and row.last_error == ""


def test_a_provider_that_answers_without_the_model_stops_immediately(db, monkeypatch):
    """Asked and told no is settled. Spending five attempts on it would be retrying a fact."""
    _answers(monkeypatch, {"claude-other"})
    _cred(db, "cred_1", model="claude-x")

    cr.run_once(db)

    row = db.get(Credential, "cred_1")
    assert row.state == cr.UNREACHABLE
    assert row.validation_attempts == 1, "a settled answer consumed more than one attempt"


def test_backoff_grows(db):
    """A fixed interval either hammers a down provider or takes two hours to notice a fixed
    one. The schedule has to widen."""
    now = datetime.now(timezone.utc)
    cred = _cred(db, "cred_1")

    seen = []
    for _ in range(cr.MAX_ATTEMPTS):
        cr.claim(db, cred, now)
        db.refresh(cred)
        seen.append(cred.next_attempt_at)
        cred.next_attempt_at = seen[-1]

    # SQLite round-trips a `DateTime(timezone=True)` column as NAIVE while Postgres returns it
    # aware. SQL comparison is unaffected (which is why `due()` works on both), but Python-side
    # arithmetic is not — so the read side is normalised here rather than the test quietly
    # depending on whichever engine it happens to run under.
    gaps = [(t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t) - now for t in seen]
    gaps = [g.total_seconds() for g in gaps]
    assert gaps == sorted(gaps) and gaps[0] < gaps[-1], gaps


# ---- the manual paths -----------------------------------------------------------------------


def test_the_button_revives_an_unreachable_row(db, monkeypatch):
    """The button asserts that something changed. Refusing to re-ask would make a resave the
    only remedy, which is a worse answer to "I just fixed the firewall"."""
    _answers(monkeypatch, {"claude-x"})
    _cred(db, "cred_1", state=cr.UNREACHABLE, attempts=cr.MAX_ATTEMPTS)

    state = cr.retry_now(db, "cred_1", "")

    assert state == cr.VALID
    assert db.get(Credential, "cred_1").state == cr.VALID


def test_the_button_is_scope_checked(db, monkeypatch):
    """A credential in another org is not retriable from here — the same boundary the resolver
    and the listing enforce, applied to the one endpoint that reaches the network on demand."""
    from app.models import Organization

    db.add(Organization(id="org_other", name="other"))
    db.commit()
    c = _cred(db, "cred_theirs")
    c.org_id = "org_other"
    db.commit()

    with pytest.raises(LookupError):
        cr.retry_now(db, "cred_theirs", "")


# ---- a probe that explodes ------------------------------------------------------------------


def test_a_probe_that_raises_is_recorded_not_propagated(db, monkeypatch):
    """`known_models` swallows its own errors today, but this does not depend on that staying
    true: an exception from the probe must land in `last_error`, not in the loop."""
    def boom(*a, **k):
        raise RuntimeError("TLS handshake exploded")

    monkeypatch.setattr(cr.probe, "known_models", boom)
    _cred(db, "cred_1")

    cr.run_once(db)  # must not raise

    row = db.get(Credential, "cred_1")
    assert row.state == cr.PENDING
    assert "TLS handshake exploded" in row.last_error


# ---- the endpoint, because a service function nothing calls is not a button ---------------


def test_the_retry_endpoint_exists_and_reports_the_new_state(client, auth, db, monkeypatch):
    """A `retry_now` nothing exposes is the GRPH-496 shape exactly: the fix written, correct,
    and unreachable from the product. The button needs a route.

    Asserted through HTTP rather than by calling the service, because "is it wired" is the
    only question this test is asking.
    """
    _answers(monkeypatch, {"claude-x"})
    _cred(db, "cred_1", state=cr.UNREACHABLE, attempts=cr.MAX_ATTEMPTS)

    r = client.post("/api/platform/credentials/cred_1/retry", headers=auth)

    assert r.status_code == 200, r.text
    assert r.json()["state"] == cr.VALID
    assert db.get(Credential, "cred_1").state == cr.VALID


def test_the_retry_endpoint_404s_for_another_scope(client, auth, db):
    from app.models import Organization

    db.add(Organization(id="org_other", name="other"))
    db.commit()
    c = _cred(db, "cred_theirs")
    c.org_id = "org_other"
    db.commit()

    r = client.post("/api/platform/credentials/cred_theirs/retry", headers=auth)

    assert r.status_code == 404


def test_the_button_defers_when_the_loop_already_took_the_attempt(db, monkeypatch):
    """The `if not claim(...)` branch, which had no test — found by a sabotage that survived.

    When the loop has just claimed this row, the button must NOT probe again. It reports where
    the row actually is instead of spending a second attempt on a question already in flight,
    and — the part that matters — instead of reporting a probe that never happened.
    """
    probed = {"n": 0}
    monkeypatch.setattr(cr.probe, "known_models",
                        lambda *a, **k: probed.__setitem__("n", probed["n"] + 1) or None)
    _cred(db, "cred_1", state=cr.PENDING, attempts=2)
    monkeypatch.setattr(cr, "claim", lambda *a, **k: False)  # the loop got there first

    state = cr.retry_now(db, "cred_1", "")

    assert probed["n"] == 0, "the button probed a row it had not claimed"
    assert state == cr.PENDING
    assert db.get(Credential, "cred_1").validation_attempts == 2, "the budget moved twice"
