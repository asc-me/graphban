"""`gbfleet bench validate`: does each task's check discriminate?

Build task: the check must FAIL at `base_sha` and PASS at the reference solution.
Review task: a planted-defect ref (expect=bounce) must make the check red; a clean ref
(expect=sign_off) must leave it green.
Checks run in a throwaway detached worktree, so the repo's own checkout is never touched.
"""

from __future__ import annotations

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


def run_check(repo: Path, ref: str, check: Check, overlay: str = "") -> tuple[bool | None, str]:
    """True when the check passes at `ref`, False when it fails, None when `ref` is unusable.

    `overlay`, a second ref, supplies the check's test files on top of `ref`: a test that
    only exists in the reference commit must still be run against the base.
    """
    with tempfile.TemporaryDirectory(prefix="gbfleet-bench-") as tmp:
        tree = Path(tmp) / "tree"
        added = _git(repo, "worktree", "add", "--detach", str(tree), ref)
        if added.returncode != 0:
            return None, f"cannot check out {ref}: {added.stderr.strip()}"
        try:
            if overlay and check.tests:
                _git(tree, "checkout", overlay, "--", *check.tests)
            # argv, not a shell: the command is split like a shell would, with no expansion.
            argv = [*shlex.split(check.command), *check.tests]
            try:
                done = subprocess.run(argv, check=False, cwd=tree, capture_output=True,
                                      text=True, timeout=CHECK_TIMEOUT_S)
            except FileNotFoundError as exc:
                return False, f"cannot run check: {exc}"
            except subprocess.TimeoutExpired:
                return False, "timed out"
            return done.returncode == 0, ""
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree))


def _build(repo: Path, task: Task, *, skip_base: bool = False) -> Verdict:
    if task.check is None:  # the loader refuses this; guard for a hand-built Task
        return Verdict(task.id, "no_check", "a build task needs a check")
    if not skip_base:
        at_base, why = run_check(repo, task.base_sha, task.check, overlay=task.reference)
        if at_base is None:
            return Verdict(task.id, "bad_ref", why)
        if at_base:
            return Verdict(task.id, "green_at_base", "the check passes before the work")
    at_ref, why = run_check(repo, task.reference, task.check)
    if at_ref is None:
        return Verdict(task.id, "bad_ref", why)
    if not at_ref:
        return Verdict(task.id, "red_at_reference", "the check fails on the reference solution")
    return Verdict(task.id, "ok")


def _review(repo: Path, task: Task) -> Verdict:
    if task.check is None:
        return Verdict(task.id, "no_check", "a review task needs a check to discriminate")
    passed, why = run_check(repo, task.ref, task.check)
    if passed is None:
        return Verdict(task.id, "bad_ref", why)
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
