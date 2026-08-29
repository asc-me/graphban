"""GRPH-598: a real grok binary, through the supervisor's own spawn machinery.

Two things were verified and the join between them was not. `test_grok_seat.py` proves
grok READS the seat this adapter writes, by asking `grok mcp doctor`. The supervisor
tests prove `spawn`, `stop`, `reap` and the progress watcher work, using Python
stand-ins. Nothing had ever put a real vendor binary through `spawn()` — and that join
is the part an operator meets first.

**What is deliberately not verified here: registration.** The seat points at an
unreachable host with an invalid key, so the child cannot reach Graphban. That is not a
limitation being worked around, it is the case worth exercising: measured on GRPH-575, a
grok child whose MCP server fails to connect **runs to completion anyway and exits 0**.
A broken seat is not a crash, it is an expensive silence, and it is the first failure a
real operator hits.

Isolated with `GROK_HOME`, carried through `Launch.env`, so the operator's own grok
config and folder-trust store are untouched (GRPH-588 established that as the mechanism
that actually works — overriding `HOME` does nothing on Windows).

Opt-in, because it starts a real vendor process which may make one model call before it
is stopped. The gate names itself in the skip reason: a run that did not happen must not
read as one that passed.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet.adapters import ADAPTERS  # noqa: E402
from gbfleet.hostos import is_owner_only  # noqa: E402
from gbfleet.seat import Seat  # noqa: E402
from gbfleet.supervisor import _instruction_file  # noqa: E402
from gbfleet.spawn import Reason, spawn, stop  # noqa: E402
from gbfleet.worktree import create, reap, seats_present  # noqa: E402

PROBE = "GBFLEET_GROK_SPAWN_PROBE"

needs_grok = pytest.mark.skipif(
    shutil.which("grok") is None, reason="no grok binary on this machine"
)
opt_in = pytest.mark.skipif(
    os.environ.get(PROBE) != "1",
    reason=f"set {PROBE}=1 — this starts a REAL grok process and may cost one model call",
)

#: Unreachable on purpose. The child must not be able to register, so that what is being
#: exercised is the spawn machinery rather than a live server.
NOWHERE = "https://graphban-does-not-resolve.invalid"


@pytest.fixture
def child_and_tree(git_repo: Path, tmp_path: Path):
    """One real grok child in one real worktree, cleaned up however the test ends."""
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    tree = create(git_repo, tmp_path / "wt", "probe", "1")
    seat = Seat(code="PROBE-NOT-REAL", server_url=NOWHERE, api_key="gb_sk_NOT_REAL")

    # The supervisor's own writer, not a hand-rolled one. Writing this file directly
    # produced an instruction the supervisor would never have produced — unrestricted,
    # because `restrict_to_owner` is called by `_instruction_file` and by nothing else.
    # The first version of this test asserted a guarantee against a file the product had
    # not written, and duly failed; the fixture was the bug, and the useful thing it
    # showed is how narrowly that protection is scoped.
    instruction = _instruction_file(tree, seat, "probe")

    log_dir = tmp_path / "logs"
    debug_file = log_dir / "debug.log"
    adapter = ADAPTERS["grok"]
    launch = adapter.launch(
        seat, tree, instruction, Path(shutil.which("grok")), debug_file=debug_file
    )
    # `Launch.env` reaches Popen, which is how the operator's real grok config stays out
    # of this. Overriding HOME would work on POSIX and do nothing on Windows.
    launch = replace(launch, env={"GROK_HOME": str(grok_home)})

    child = spawn(launch, tree.path, tree.branch, log_dir, base=tree.base)
    try:
        yield child, tree, debug_file
    finally:
        if child.running:
            stop(child, Reason.SHUTDOWN, grace=10)
        reap(tree)


@needs_grok
@opt_in
def test_the_seat_and_instruction_reach_a_real_child_protected(child_and_tree):
    """Written by `spawn`, not by a probe — the difference this ticket exists to close."""
    child, tree, _ = child_and_tree

    seat_file = tree.path / ".grok" / "config.toml"
    assert seat_file.exists(), f"spawn did not write the seat; found {seats_present(tree.path)}"
    assert "gb_sk_NOT_REAL" in seat_file.read_text(encoding="utf-8")
    assert is_owner_only(seat_file), "a live api key was left readable by others"

    instruction = tree.path / ".gbfleet-instruction"
    assert instruction.exists()
    assert is_owner_only(instruction), "the enrolment code was left readable by others"


@needs_grok
@opt_in
def test_the_debug_flag_produces_an_actual_file(child_and_tree):
    """`--debug-file` has only ever been checked as a string in argv.

    A flag that is passed and produces nothing is indistinguishable from one that was
    never passed, and an operator who turned debug on would be reading an empty
    directory wondering which.
    """
    child, _, debug_file = child_and_tree

    deadline = time.monotonic() + 30
    while not debug_file.exists() and time.monotonic() < deadline:
        if not child.running:
            break
        time.sleep(0.2)

    assert debug_file.exists(), (
        f"grok was launched with --debug-file {debug_file} and wrote nothing there. "
        "The flag reaches argv; this is whether it reaches the disk."
    )


@needs_grok
@opt_in
def test_the_supervisor_sees_a_real_vendor_producing_output(child_and_tree):
    """The progress watcher against a real vendor rather than a stand-in that was
    written to be observable."""
    child, _, _ = child_and_tree
    assert child.output is not None

    deadline = time.monotonic() + 30
    reading = child.output.sample(time.monotonic())
    while reading.total_bytes == 0 and time.monotonic() < deadline:
        time.sleep(0.2)
        reading = child.output.sample(time.monotonic())

    assert reading.total_bytes > 0, (
        "a real grok child ran for 30s and the watcher saw nothing at all — which is "
        "what it would also report for a child that never started"
    )
    assert reading.watched >= 2, "stdout and stderr were not both being watched"


@needs_grok
@opt_in
def test_stopping_a_real_vendor_process_works_and_reap_takes_the_credential_away(
    git_repo: Path, tmp_path: Path
):
    """Not using the fixture: this one owns the whole lifecycle, because what it is
    checking is the end of it."""
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    tree = create(git_repo, tmp_path / "wt", "probe", "2")
    seat = Seat(code="PROBE-NOT-REAL", server_url=NOWHERE, api_key="gb_sk_NOT_REAL")
    instruction = _instruction_file(tree, seat, "probe")

    adapter = ADAPTERS["grok"]
    launch = replace(
        adapter.launch(seat, tree, instruction, Path(shutil.which("grok"))),
        env={"GROK_HOME": str(grok_home)},
    )
    child = spawn(launch, tree.path, tree.branch, tmp_path / "logs", base=tree.base)

    assert child.running, "the child was gone before it could be stopped"
    result = stop(child, Reason.SHUTDOWN, grace=15)

    assert not child.running, f"a real grok process survived stop(): {result}"

    reaped = reap(tree)
    assert not (tree.path / ".grok" / "config.toml").exists(), (
        f"the seat outlived the worktree ({reaped.disposition.value}) — a live api key "
        "left on disk after the child that held it is gone"
    )
