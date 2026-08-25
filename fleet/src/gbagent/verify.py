"""`run_tests` and `git_diff` — the only two tools that start a process (PRD-24 D3, D4, S2).

**There is no general shell.** These run a command the repository declared and a fixed `git`
invocation, both without a shell, so a `command` containing a pipe gets those characters as
literal arguments and fails loudly rather than quietly running two things. Everything else a
shell would buy costs the boundary S1 exists to establish, and the rule for changing that is in
D4: somebody names a task that genuinely cannot be done without one, and records it.

**The exit code is the truth; the counts are a reading.** `ok` comes from the process, which
cannot be wrong. `passed`, `failed` and `failed_tests` come from parsing output whose shape
depends on a runner this module does not choose — so when they cannot be read they are `None`
and an empty list, never `0`. A `failed: 0` on a run that failed would be the worst kind of
answer: confident, structured, and false. The tail is always there, so a model can read what a
parser could not.

The parser understands pytest's summary shape, which is what this repository declares. Anything
else degrades to exit code plus tail rather than guessing.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import VerifyConfig
from .workspace import ToolError, safe_path

#: Lines of output handed back on failure. Enough to act on; bounded so one broken run cannot
#: fill the window that D7's compaction exists to protect.
TAIL_LINES = 100

#: An unattended agent that hangs is worse than one that fails: D6's turn budget cannot help
#: if a single turn never returns. This repository's own backend suite takes ~9 minutes.
DEFAULT_TIMEOUT = 1800

_SUMMARY = re.compile(r"^=*\s*(?:(\d+) failed)?.*?(?:(\d+) passed)?.*?in [\d.]+s", re.M)
_COUNT = re.compile(r"(\d+)\s+(passed|failed|error|errors)")
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)


def _read_counts(out: str) -> tuple[int | None, int | None, list[str]]:
    """Best effort. Returns (passed, failed, names) with `None` where nothing could be read."""
    names = _FAILED_LINE.findall(out)
    tallies = {kind: int(n) for n, kind in _COUNT.findall(out)}
    # `.get` WITHOUT a default is the whole mechanism: an absent count stays None and never
    # becomes 0. Adding `, 0` here would report a clean run on output nobody could read —
    # confident, structured and false. An earlier draft had an explicit `if nothing parsed:
    # return None, None, []` guard above this, which a sabotage showed was dead code saying
    # the same thing twice; the comment is the part that was load-bearing.
    passed = tallies.get("passed")
    failed = tallies.get("failed")
    if failed is None and ("error" in tallies or "errors" in tallies):
        failed = tallies.get("error", tallies.get("errors"))
    return passed, failed, names


def run_tests(root: Path, cfg: VerifyConfig, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run the declared command and report what happened.

    The agent does not compose test commands — a composed command is a turn spent and a chance
    to run the wrong thing (D3). It runs this one, or the config refused before spawn.
    """
    try:
        proc = subprocess.run(
            cfg.argv, cwd=str(cfg.cwd), capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        # `config.load` checked this before spawn; reaching here means the tree changed under
        # a running agent, which is worth saying plainly rather than as a traceback.
        raise ToolError(f"{cfg.argv[0]!r} is gone — it was there when this agent started") from None
    except subprocess.TimeoutExpired:
        raise ToolError(
            f"the test command exceeded {timeout}s and was killed. Nothing is known about "
            "whether it would have passed."
        ) from None

    out = (proc.stdout or "") + (proc.stderr or "")
    lines = out.splitlines()
    passed, failed, names = _read_counts(out)
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        # None, never 0, when the output could not be read — see the module docstring.
        "passed": passed,
        "failed": failed,
        "failed_tests": names[:20],
        "command": cfg.source,
        "tail": "\n".join(lines[-TAIL_LINES:]),
        "truncated": len(lines) > TAIL_LINES,
    }


def git_diff(root: Path, *, path: str = ".") -> dict:
    """What this agent has changed, so far, in its own worktree.

    Scoped through the same boundary as every other path (S1): `git_diff` on `../..` would
    otherwise report a neighbouring worktree's work as this agent's.

    **New files count, and they are most of the work** (GRPH-488). `git diff` reports
    tracked, unstaged changes only — and every file this agent creates is untracked, so a
    plain diff cannot see the output of `write_file`, the tool the agent uses most. Worse
    than the empty answer was the mixed one: create a module, edit a tracked file, and you
    got a confident, well-formed diff that silently omitted the new module. A tool that says
    "nothing" is obviously wrong; one that shows you most of your work reads as complete.

    That is the answer this module's own docstring rejects for test counts — "confident,
    structured, and false" — so it should not have been acceptable one function below it.

    `git add -N` (intent-to-add) is what makes them visible: it records the path in the index
    without staging content, so the diff includes them while `git diff --cached` stays empty.
    Nothing becomes staged for commit and D9's dirty-worktree salvage is untouched — asserted
    by a test, because "it does not stage anything" is exactly the kind of claim that rots.
    """
    target = safe_path(root, path)
    base = str(safe_path(root, "."))
    # Intent-to-add first, so new files reach the diff below. Failure is deliberately
    # ignored rather than raised: a tree that is not a git repository, or a path git
    # declines to add, must still produce whatever diff it can. The diff call below is the
    # one that reports trouble, and it already does.
    subprocess.run(["git", "add", "-N", "--", str(target)],
                   cwd=base, capture_output=True, text=True, timeout=120)
    try:
        proc = subprocess.run(
            ["git", "diff", "--", str(target)],
            cwd=base, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ToolError(f"git diff failed: {e}") from None
    if proc.returncode != 0:
        raise ToolError(f"git diff failed: {(proc.stderr or '').strip()[:200]}")

    lines = (proc.stdout or "").splitlines()
    return {
        "path": path,
        "empty": not lines,
        "lines": len(lines),
        "diff": "\n".join(lines[: TAIL_LINES * 4]),
        "truncated": len(lines) > TAIL_LINES * 4,
    }
