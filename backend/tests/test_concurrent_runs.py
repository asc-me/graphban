"""Two test runs in one working tree must not share a SQLite file (GRPH-554).

**The mechanism, finally.** That item recorded three unrelated-looking signatures over weeks
and concluded the cause was unknown. It is one cause. The suite DELETES and rebuilds its
database, so a second run in the same worktree unlinks the file the first has open, and every
signature follows from that single act:

* an unlinked inode under an open connection -> `attempt to write a readonly database`,
  `disk I/O error`, `malformed database schema (X) - table X already exists`
* a new connection creating a fresh empty file -> `no such table: projects`
* a re-seed landing in a half-populated file -> `UNIQUE constraint failed: projects.tag`

Read as three problems they never resolved into one; read as one they are obvious.

**Postgres was never affected, and that is the tell.** GRPH-534 gave it `refuse_if_in_use`.
SQLite got nothing, and `_drop_worker_database` returns early on it — which that item's own
text notes without drawing the conclusion.

Reproduced deliberately before fixing: two suites started four seconds apart, each of which
passes alone (44 and 52 passed), corrupted BOTH runs — 2 failed / 16 errors in the second and
7 failed / 17 errors in the first, the one that was minding its own business. That asymmetry
is why it was so hard to attribute: the damage lands on whichever run happens to be reading,
which is usually not the one that caused it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.dbnames import claim_sqlite, release_sqlite


@pytest.fixture()
def scratch(tmp_path) -> str:
    return f"sqlite:///{tmp_path}/scratch.db"


# ── the guard fires, and only where it should ─────────────────────────────────

def test_a_free_database_is_claimed(scratch):
    try:
        assert claim_sqlite(scratch) is True
        assert Path(scratch.split("///")[1] + ".lock").exists()
    finally:
        release_sqlite(scratch)


def test_postgres_is_not_claimed_here():
    """Postgres has its own guard (`refuse_if_in_use`, GRPH-534) and its own reason — a
    server, not a file. Claiming it here would be a second mechanism for one job, and the
    two would disagree the first time either changed."""
    assert claim_sqlite("postgresql+psycopg://u:p@localhost:5544/graphban_test") is False
    assert claim_sqlite("postgresql://localhost/x") is False


def test_claiming_twice_in_one_process_is_fine(scratch):
    """Re-entrant on purpose. conftest rewrites the environment at IMPORT time and pytest can
    load it more than once — that is the documented `graphban_test_gw0_gw0` incident. A guard
    that refused its own process would turn a known quirk into a hard failure."""
    try:
        assert claim_sqlite(scratch) is True
        assert claim_sqlite(scratch) is True
    finally:
        release_sqlite(scratch)


# ── the case the whole thing exists for ───────────────────────────────────────

def _holder_script(path: str) -> str:
    return textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from tests.dbnames import claim_sqlite
        claim_sqlite({f"sqlite:///{path}"!r})
        print("HELD", flush=True)
        time.sleep(30)
    """)


def test_a_second_process_is_refused(scratch, tmp_path):
    """THE test. A real second process, because that is the real scenario — a background
    suite still running while another is started in the same worktree.

    In-process locking would prove nothing: `flock` is held per open file description, so a
    same-process check exercises different machinery than the one that matters.
    """
    path = scratch.split("///")[1]
    holder = subprocess.Popen([sys.executable, "-c", _holder_script(path)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD", "the holder never took the lock"

        with pytest.raises(RuntimeError) as err:
            claim_sqlite(scratch)

        message = str(err.value)
        assert "another test run already holds" in message
        assert path in message, "the refusal does not say WHICH database"
        assert "DATABASE_URL" in message, (
            "the refusal does not say how to proceed — a guard that only says no is one "
            "people work around rather than obey")
        assert "GRPH-554" in message, "the refusal does not point at the explanation"
    finally:
        holder.kill()
        holder.wait()
        release_sqlite(scratch)


def test_the_claim_is_released_when_the_holder_dies(scratch):
    """`flock` rather than a PID file, and this is why: the kernel releases it however the
    process ends. A PID file has to be tidied up by the thing that crashed, and a stale one
    refuses every future run — which turns a real guard into something people delete by
    habit, and then it is not a guard."""
    path = scratch.split("///")[1]
    holder = subprocess.Popen([sys.executable, "-c", _holder_script(path)],
                              stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "HELD"
    holder.kill()
    holder.wait()

    try:
        assert claim_sqlite(scratch) is True, (
            "the lock outlived the process that held it — a killed run would block every "
            "run after it")
    finally:
        release_sqlite(scratch)


def test_releasing_lets_the_next_run_in(scratch):
    """The complement of the refusal. A guard that never releases is a guard that breaks the
    next honest run, and it would satisfy the refusal test above."""
    assert claim_sqlite(scratch) is True
    release_sqlite(scratch)
    try:
        assert claim_sqlite(scratch) is True
    finally:
        release_sqlite(scratch)


def test_the_session_setup_actually_claims_the_sqlite_file():
    """THE CALL. Concurrent-run tests drive claim_sqlite directly, so deleting
    claim_sqlite(...) from conftest left them green (GRPH-554 bounce). A second
    pytest in the same worktree would again unlink .pytest.db under an open connection.
    """
    src = Path(__file__).resolve().parent.joinpath("conftest.py").read_text()
    live = [ln for ln in src.splitlines()
            if "claim_sqlite" in ln and not ln.lstrip().startswith("#")]
    assert any("claim_sqlite(os.environ" in ln for ln in live), (
        "conftest no longer claims the SQLite file this session will open — the "
        "helper can be correct and two pytest processes still share one database. "
        "A comment is not the claim."
    )
