"""`gbfleet bench validate`: does each task's check discriminate?

Build task: the check must FAIL at `base_sha` and PASS at the reference solution.
Review task: a planted-defect ref (expect=bounce) must make the check red; a clean ref
(expect=sign_off) must leave it green.
Checks run in a throwaway detached worktree, so the repo's own checkout is never touched.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .suite import Check, Suite, Task

CHECK_TIMEOUT_S = 600


@dataclass(frozen=True)
class Verdict:
    task: str
    status: str  # "ok" or a reason: green_at_base, red_at_reference, ...
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def line(self) -> str:
        return f"{self.task}: {self.status}" + (f" ({self.detail})" if self.detail else "")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True)


def split_command(command: str, *, windows: bool | None = None) -> list[str]:
    """Split a check command into argv the way the host's shell would quote it.

    POSIX mode strips backslashes, which would turn `C:\\x\\python.exe` into `C:xpython.exe`,
    so on Windows split in non-POSIX mode (backslashes kept) and drop the quotes it leaves
    around a quoted token.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return shlex.split(command)
    out = []
    for tok in shlex.split(command, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


PASS, FAIL, BAD_REF, UNRUNNABLE = "pass", "fail", "bad_ref", "check_unrunnable"
BAD_OVERLAY, TIMED_OUT = "bad_overlay", "check_timed_out"
# Outcomes that say nothing about the work: none of them may count as a red or green check.
INCONCLUSIVE = (BAD_REF, UNRUNNABLE, BAD_OVERLAY, TIMED_OUT)


def run_check(repo: Path, ref: str, check: Check, overlay: str = "") -> tuple[str, str]:
    """PASS or FAIL for the check at `ref`; BAD_REF when `ref` cannot be checked out;
    UNRUNNABLE when the command cannot start (a missing binary is not a red check, so
    it must never count as discriminating); BAD_OVERLAY when the test files cannot be laid
    over `ref`; TIMED_OUT when the check outlives CHECK_TIMEOUT_S (a hang is not a red check).

    `overlay`, a second ref, supplies the check's test files on top of `ref`: a test that
    only exists in the reference commit must still be run against the base.
    """
    with tempfile.TemporaryDirectory(prefix="gbfleet-bench-") as tmp:
        tree = Path(tmp) / "tree"
        added = _git(repo, "worktree", "add", "--detach", str(tree), ref)
        if added.returncode != 0:
            return BAD_REF, f"cannot check out {ref}: {added.stderr.strip()}"
        try:
            if overlay and check.tests:
                laid = _git(tree, "checkout", overlay, "--", *check.tests)
                if laid.returncode != 0:
                    return BAD_OVERLAY, f"cannot lay tests from {overlay}: {laid.stderr.strip()}"
            # argv, not a shell: the command is split like a shell would, with no expansion.
            argv = [*split_command(check.command), *check.tests]
            try:
                done = subprocess.run(argv, check=False, cwd=tree, capture_output=True,
                                      text=True, timeout=CHECK_TIMEOUT_S)
            except FileNotFoundError as exc:
                return UNRUNNABLE, f"cannot run check: {exc}"
            except subprocess.TimeoutExpired:
                return TIMED_OUT, f"check ran past {CHECK_TIMEOUT_S}s"
            return (PASS if done.returncode == 0 else FAIL), ""
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree))


def _build(repo: Path, task: Task, *, skip_base: bool = False) -> Verdict:
    if task.check is None:  # the loader refuses this; guard for a hand-built Task
        return Verdict(task.id, "no_check", "a build task needs a check")
    if not skip_base:
        at_base, why = run_check(repo, task.base_sha, task.check, overlay=task.reference)
        if at_base in INCONCLUSIVE:
            return Verdict(task.id, at_base, why)
        if at_base == PASS:
            return Verdict(task.id, "green_at_base", "the check passes before the work")
    at_ref, why = run_check(repo, task.reference, task.check)
    if at_ref in INCONCLUSIVE:
        return Verdict(task.id, at_ref, why)
    if at_ref == FAIL:
        return Verdict(task.id, "red_at_reference", "the check fails on the reference solution")
    return Verdict(task.id, "ok")


def _review(repo: Path, task: Task) -> Verdict:
    if task.check is None:
        return Verdict(task.id, "no_check", "a review task needs a check to discriminate")
    outcome, why = run_check(repo, task.ref, task.check)
    if outcome in INCONCLUSIVE:
        return Verdict(task.id, outcome, why)
    passed = outcome == PASS
    if task.expect == "bounce" and passed:
        return Verdict(task.id, "green_at_defect", "the planted defect leaves every check green")
    if task.expect == "sign_off" and not passed:
        return Verdict(task.id, "red_at_clean", "the clean ref makes a check red")
    return Verdict(task.id, "ok")


def validate(suite: Suite, repo: Path, *, skip_base: bool = False) -> list[Verdict]:
    """`skip_base` exists only so a test can sabotage the at-base run and watch it fail."""
    return [
        _build(repo, t, skip_base=skip_base) if t.kind == "build" else _review(repo, t)
        for t in suite.tasks
    ]
