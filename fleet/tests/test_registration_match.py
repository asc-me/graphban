"""A child is matched to its roster row by worktree, and the two spellings must resolve alike.

Found by the PRD-36 criterion-18 check: `--workspace ../agentledger-p36check` left the
supervisor holding `.../agentledger/../agentledger-p36check/p36check-5` while the child
reported the realpath. Same directory, two strings; a registered child reported
never_registered and killed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from gbfleet import spawn as spawn_mod


class _Proc:
    returncode = None
    def poll(self): return None


def _child(worktree: str) -> spawn_mod.Child:
    kw = dict(adapter="fake", worktree=Path(worktree), branch="gb/w-1", base="main",
              seat_path=Path("/tmp/seat"), process=_Proc(), started_at=time.monotonic(),
              log_dir=Path("/tmp/logs"))
    return spawn_mod.Child(**kw)


def test_a_dotdot_workspace_still_matches_the_childs_resolved_worktree(tmp_path: Path):
    real = tmp_path / "ws" / "p-1"
    real.mkdir(parents=True)
    unresolved = str(tmp_path / "repo" / ".." / "ws" / "p-1")
    child = _child(unresolved)
    roster = {"agents": [{"id": "GRPH-A9", "worktree": os.path.realpath(str(real)),
                          "enrolment_id": "seat-1", "assigned": {"item": "GRPH-1", "state": "claimed"}}]}
    got = spawn_mod.await_registration(child, lambda: roster, window=5, poll=0, sleep=lambda _: None)
    assert got["id"] == "GRPH-A9" and child.agent_id == "GRPH-A9"
    assert child.assigned == {"item": "GRPH-1", "state": "claimed"}


def test_a_different_directory_does_not_match(tmp_path: Path):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    child = _child(str(tmp_path / "a"))
    child.process.returncode = 0  # exited: the window must not be waited out
    class Dead(_Proc):
        returncode = 0
    child.process = Dead()
    roster = {"agents": [{"id": "GRPH-A9", "worktree": str(tmp_path / "b")}]}
    try:
        spawn_mod.await_registration(child, lambda: roster, window=1, poll=0, sleep=lambda _: None)
    except Exception:
        pass
    assert child.agent_id is None
