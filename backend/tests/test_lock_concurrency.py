"""Every `FOR UPDATE` in this codebase, raced for real (GRPH-432).

`with_for_update()` is a **no-op on SQLite**, and SQLite is what the default suite runs on.
So before this file, every lock in the tree could be deleted with the whole suite still
green — including ones whose own docstrings said the lock was the entire point. Measured
during the review that filed this: removing the floor-invariant lock left 16/16 passing,
and removing the recompute lock left 13/13.

A two-session test written the obvious way would not have helped either: SQLite serializes
writers globally, so the invariant holds there with or without the lock. **Pinning these
needs Postgres, two real connections, and a deliberate interleaving.**

Which creates the trap this file has to avoid: a suite that skips on the default engine
reads as green when it ran nothing. `test_every_lock_has_a_race` therefore runs on BOTH
engines and fails if a `with_for_update` exists that nothing here claims — so a new lock
cannot be added silently, even by someone who only ever runs SQLite.
"""
import re
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal, engine
from app.models import Membership, OrgMembership, Organization, Project, User

APP = Path(__file__).resolve().parents[1] / "app"

#: Each `with_for_update` site, and the test below that races it. A lock with no entry
#: fails `test_every_lock_has_a_race` — on either engine.
RACED = {
    "services/orgs.py": "test_the_floor_check_locks_the_seats_it_counts",
    "services/teams.py": "test_recompute_locks_the_project_it_materialises_into",
    "services/keys.py": "test_minting_a_key_locks_the_project_it_numbers",
}

postgres_only = pytest.mark.skipif(
    not engine.url.drivername.startswith("postgresql"),
    reason=(
        "LOCKS UNVERIFIED ON THIS ENGINE. `with_for_update` is a no-op on SQLite, so these "
        "races prove nothing here — they pass with the lock removed. Run against Postgres "
        "(DATABASE_URL=postgresql+psycopg://…) to exercise them; CI does."
    ),
)


def _blocks_while_row_is_locked(lock_sql: str, call, timeout_ms: int = 400) -> bool:
    """Does `call` try to take a row lock that somebody else already holds?

    **Racing two threads and hoping they interleave does not work here**, and the first
    version of this file proved it: with a barrier at thread start, all three races passed
    with their locks REMOVED. The window between a read and its write is microseconds, so
    the interleaving essentially never happens and the test vouches for nothing — the exact
    shape GRPH-432 was filed about.

    So the contention is made certain rather than hoped for. One session holds a row lock;
    the other runs the real code path under a short `lock_timeout`. If that path locks the
    same row it waits and times out; if the lock was removed it sails through. The timeout
    IS the assertion.

    **Choosing WHICH lock to hold is the whole difficulty**, and the second version of this
    file got it wrong too. Holding `FOR UPDATE` on a project row blocks anything that merely
    INSERTS a row referencing it, because the foreign key takes `FOR KEY SHARE` and the two
    conflict — so every one of these passed with its lock removed, measuring "does this
    write a child row" instead of "does this take the lock".

    Two discriminators, one per shape:

    - `FOR NO KEY UPDATE` on the parent. It is compatible with the `FOR KEY SHARE` an FK
      insert takes, and conflicts with the explicit `FOR UPDATE` the code takes. So only
      the real lock contends.
    - For the seat count, hold a row the function does not otherwise touch — lock the
      OTHER administrator's seat and demote this one. Only the counting query reads it.
    """
    holder = SessionLocal()
    victim = SessionLocal()
    try:
        holder.execute(text(lock_sql))          # held until rollback below
        victim.execute(text(f"SET lock_timeout = '{timeout_ms}ms'"))
        try:
            call(victim)
            victim.commit()
            return False                        # never contended — no lock in the path
        except OperationalError:
            return True                         # blocked on the held row
        finally:
            victim.rollback()
    finally:
        holder.rollback()
        holder.close()
        victim.close()


def test_every_lock_has_a_race():
    """Runs on BOTH engines. The point of the file: a lock nothing races is a comment.

    This is what stops the Postgres-only tests below from being invisible. If they skip and
    nothing else runs, a new `with_for_update` still fails here — on SQLite, in the default
    suite, where somebody will actually see it.
    """
    sites = sorted(
        str(p.relative_to(APP))
        for p in APP.rglob("*.py")
        if re.search(r"with_for_update", p.read_text())
    )
    # Reported when it skips, because a Postgres-only suite that silently skips reads as
    # green when it ran nothing — the same absence-reads-as-clean shape it exists to fix.
    if not engine.url.drivername.startswith("postgresql"):
        print(
            "\n  LOCKS NOT EXERCISED on this engine — `with_for_update` is a no-op on "
            f"SQLite. Unverified here: {sorted(RACED)}. CI runs them on Postgres."
        )

    unraced = [s for s in sites if s not in RACED]
    assert not unraced, (
        f"these hold FOR UPDATE and nothing in this file races them: {unraced}. "
        "Add a race, or delete the lock — an untested lock is a comment that costs a "
        "round trip."
    )
    # And the reverse, so RACED cannot rot into claims about code that is gone.
    stale = [s for s in RACED if s not in sites]
    assert not stale, f"RACED names sites with no lock any more: {stale}"


@pytest.fixture()
def org(_clean_database):
    """An org with TWO administrators and one project — the smallest world these races need."""
    s = SessionLocal()
    try:
        for uid, email in (("lk_a", "a@lock.example.com"), ("lk_b", "b@lock.example.com")):
            s.add(User(id=uid, name=uid, handle=uid, email=email, password_hash="x"))
        s.add(Organization(id="lk_org", name="Locks", plan="free"))
        s.flush()
        s.add(Project(id="lk_prj", name="Locked", tag="LK", org_id="lk_org"))
        for uid in ("lk_a", "lk_b"):
            s.add(OrgMembership(org_id="lk_org", user_id=uid, role="admin"))
        s.commit()
    finally:
        s.close()
    return {"org": "lk_org", "project": "lk_prj", "a": "lk_a", "b": "lk_b"}


@postgres_only
def test_the_floor_check_locks_the_seats_it_counts(org):
    """`_refuse_if_last_administrator` counts administrators and then lets a demotion
    proceed. Its docstring says the FOR UPDATE is "the whole difference between this check
    and a comment" — this is what makes that checkable.

    Sequentially the floor is unreachable: `you cannot change your own role` means A demotes
    B but never A, so somebody always remains. The only route to zero is two demotions
    overlapping, and the lock is what stops them.
    """
    from app.services import orgs as orgs_svc

    def demote_b(s):
        orgs_svc.set_member_role(s, org["org"], org["b"], "member",
                                 actor=s.get(User, org["a"]))

    # A's seat, while demoting B: the demotion itself never writes A's row, so the only
    # thing that can contend is `_administrators(lock=True)` counting the seats.
    assert _blocks_while_row_is_locked(
        "SELECT id FROM org_memberships WHERE org_id = 'lk_org' AND user_id = 'lk_a' FOR UPDATE",
        demote_b,
    ), "the demotion path never contended for the seats it counts — the lock is absent"


@postgres_only
def test_recompute_locks_the_project_it_materialises_into(org):
    """A (user, project) pair with no row yet offers nothing to lock, so `recompute` locks
    the project — the row that always exists. Two grant changes touching one project queue
    there instead of both reading `None` and both inserting."""
    from app.models import Team, TeamGrant, TeamMember
    from app.services import teams as teams_svc

    s = SessionLocal()
    try:
        s.add(Team(id="lk_team", org_id=org["org"], name="T"))
        s.flush()
        s.add(TeamMember(team_id="lk_team", user_id=org["a"]))
        s.add(TeamGrant(team_id="lk_team", project_id=org["project"], access="write"))
        s.commit()
    finally:
        s.close()

    assert _blocks_while_row_is_locked(
        # NO KEY UPDATE: an FK insert takes KEY SHARE and would not conflict, so a pass
        # here means the explicit lock ran rather than a membership row being written.
        "SELECT id FROM projects WHERE id = 'lk_prj' FOR NO KEY UPDATE",
        lambda v: teams_svc.recompute(v, org["a"], org["project"]),
    ), "recompute never contended for the project row — the lock is absent"


@postgres_only
def test_minting_a_key_locks_the_project_it_numbers(org):
    """`keys._lock_project` serializes numbering per project, so two creates cannot take the
    same number. Keys are what a human cites, so a collision is two items answering to one
    name."""
    from app.services import items as items_svc

    assert _blocks_while_row_is_locked(
        "SELECT id FROM projects WHERE id = 'lk_prj' FOR NO KEY UPDATE",
        lambda v: items_svc.create_item(v, title="raced", project_id=org["project"]),
    ), "the mint path never contended for the project row — the lock is absent"
