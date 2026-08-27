"""The bounded retry behind `pending_validation` (PRD-25 S2b, GRPH-521).

A credential that could not be ASKED is not a credential that answered and said no. The second
is settled and gets a 422 at save (GRPH-485, unchanged). The first is a fact about the network
five seconds ago, and this is what re-asks it.

**Bounded, and the bound is the point.** Five attempts, ~30s out to two hours. `unreachable`
exists so a row never sits claiming a retry that will not happen — a state that says "we will
try again" and then never does is worse than one that admits it has stopped.

**Progress lives in the row, never in the loop.** `validation_attempts` and `next_attempt_at`
are the whole of the scheduler's memory, which is what makes a restart mid-backoff a non-event:
the process comes back, reads the row, and continues from where the row says it is. Nothing is
reconstructed and nothing is lost.

**One attempt at a time via a conditional update, not a lock.** The loop, a button click and a
resave can all decide the same row is due at the same moment. The claim is:

    UPDATE credentials SET validation_attempts = validation_attempts + 1,
                           next_attempt_at = <now + backoff>
     WHERE id = ? AND next_attempt_at IS NOT DISTINCT FROM <the value I read>

Zero rows updated means somebody else got there first. No locks, no queue — the token is a
column the schedule already needed.

**The claim also SCHEDULES.** One write does both, so a row is never left simultaneously
unclaimed and unscheduled. If the probe then fails, nothing more needs writing: the next attempt
is already on the row. That ordering is why a crash between claiming and probing costs one
attempt from the budget and nothing else.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.models import Credential
from app.providers import probe
from app.security import secrets

logger = logging.getLogger("graphban.credential_retry")

#: Seconds before each subsequent attempt. Five entries, five attempts — 30s catches a restart,
#: two hours catches an outage, and the middle covers the ordinary case of somebody fixing a
#: firewall rule. Deliberately not exponential-with-jitter: this is one deployment's own
#: providers, not a fleet stampeding a shared endpoint (a grill non-goal).
BACKOFF = (30, 120, 600, 1800, 7200)

MAX_ATTEMPTS = len(BACKOFF)

PENDING = "pending_validation"
VALID = "valid"
UNREACHABLE = "unreachable"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def due(db: Session, now: datetime | None = None) -> list[Credential]:
    """Rows whose next attempt has come around.

    `next_attempt_at IS NULL` is included and means "never scheduled" — a row saved while the
    provider was unreachable is due immediately, not after a first backoff it never earned.
    """
    now = now or _now()
    return list(
        db.query(Credential)
        .filter(Credential.state == PENDING)
        .filter(or_(Credential.next_attempt_at.is_(None), Credential.next_attempt_at <= now))
        .order_by(Credential.id)
        .all()
    )


def claim(db: Session, cred: Credential, now: datetime | None = None) -> bool:
    """Take the attempt, scheduling the next one in the same write. True if we got it.

    **The NULL case is why this is not the one-line UPDATE the ticket sketches.** A row that
    has never been attempted has `next_attempt_at IS NULL`, and `NULL = NULL` is false in SQL —
    so the obvious `WHERE next_attempt_at = :value_i_read` can never claim a fresh row, and the
    first attempt would never happen. The comparison has to be IS NOT DISTINCT FROM, spelled
    here as an explicit two-branch predicate because SQLite has no such operator.
    """
    now = now or _now()
    seen = cred.next_attempt_at
    attempt_index = min(cred.validation_attempts, MAX_ATTEMPTS - 1)
    nxt = now + timedelta(seconds=BACKOFF[attempt_index])

    predicate = (
        Credential.next_attempt_at.is_(None) if seen is None
        else Credential.next_attempt_at == seen
    )
    result = db.execute(
        update(Credential)
        .where(Credential.id == cred.id, Credential.state == PENDING, predicate)
        .values(validation_attempts=Credential.validation_attempts + 1, next_attempt_at=nxt)
    )
    db.commit()
    return result.rowcount == 1


def attempt(db: Session, credential_id: str, now: datetime | None = None) -> str:
    """Probe one credential and record what happened. Returns the resulting state.

    Called after a successful `claim`, so the budget is already spent and the next attempt is
    already scheduled. This only has to record the ANSWER.
    """
    cred = db.get(Credential, credential_id)
    if cred is None:
        return ""

    try:
        known = probe.known_models(cred.kind, cred.base_url or "",
                                   secrets.decrypt(cred.api_key) if cred.api_key else "")
    except Exception as exc:  # noqa: BLE001 — a probe must never propagate into the loop
        known, exc_text = None, str(exc)
        cred.last_error = exc_text[:500]
    else:
        exc_text = ""

    if known is not None and (not cred.model or cred.model in known):
        cred.state = VALID
        cred.last_error = ""
        cred.next_attempt_at = None
        db.commit()
        return VALID

    if known is not None:
        # It answered and does not have the model. Retrying cannot change that, so the budget
        # is spent immediately rather than five times over a settled answer.
        cred.state = UNREACHABLE
        cred.last_error = f"{cred.kind} does not have model {cred.model!r}"
        cred.next_attempt_at = None
        db.commit()
        return UNREACHABLE

    if not cred.last_error:
        cred.last_error = f"{cred.kind} at {cred.base_url or '(no endpoint)'} could not be asked"
    if cred.validation_attempts >= MAX_ATTEMPTS:
        # Budget spent. `next_attempt_at` is cleared so nothing reads this row as scheduled —
        # a row that says it will try again and will not is the thing this state prevents.
        cred.state = UNREACHABLE
        cred.next_attempt_at = None
    db.commit()
    return cred.state


def run_once(db: Session, now: datetime | None = None) -> int:
    """One pass over everything due. Returns how many attempts were actually made."""
    now = now or _now()
    made = 0
    for cred in due(db, now):
        if claim(db, cred, now):
            attempt(db, cred.id, now)
            made += 1
    return made


def retry_now(db: Session, credential_id: str, scope: str) -> str:
    """The `Test connection` button, and the same conditional shape as the loop.

    A click races the scheduler by construction — the operator presses it precisely when a row
    looks stuck, which is when the loop is also most likely to be picking it up. Going through
    `claim` means the budget decrements once, whoever wins.

    A row that is already `unreachable` is reset to pending first: the button is an assertion
    that something changed, and refusing to re-ask would make the only remedy a resave.
    """
    cred = db.get(Credential, credential_id)
    if cred is None or (cred.org_id or "") != scope:
        raise LookupError(credential_id)
    if cred.state == UNREACHABLE:
        cred.validation_attempts = 0
        cred.next_attempt_at = None
        cred.state = PENDING
        db.commit()
        db.refresh(cred)
    if not claim(db, cred):
        # The loop took this attempt a moment ago. Report where the row actually is rather
        # than claiming a probe that did not happen.
        db.refresh(cred)
        return cred.state
    return attempt(db, credential_id)
