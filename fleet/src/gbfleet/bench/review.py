"""Seeded-defect review tasks (GRPH-P98 section 8).

A review task hands a reviewer a ref to review against its brief: the planted-defect ref
(`expect=bounce`) or the clean ref (`expect=sign_off`). The reviewer's verdict is recorded
as the task's outcome and scored against `expect` by `score.score_run`.

Before a reviewer runs, the task is validated: the planted ref must make its check red and
the clean ref must leave it green. A task that fails that is `harness_error`, never a verdict.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import validate as validate_mod
from .harvest import Candidate, Dropped
from .score import REVIEW_NON_VERDICTS, REVIEW_VERDICTS, TaskOutcome
from .suite import Task

MUTATION_NOT_APPLICABLE = "mutation_not_applicable"
DEFECT_REF_PREFIX = "refs/gbfleet-bench"

#: A reviewer adapter: given the task, returns "bounce", "sign_off", or a non-verdict
#: outcome (`never_registered`, `timed_out`, `no_verdict`). Raising is `harness_error`.
Reviewer = Callable[[Task], str]


@dataclass
class ReviewResult:
    task_id: str
    outcome: str
    detail: str = ""
    wall_seconds: float | None = None


def preflight(repo: Path, task: Task) -> validate_mod.Verdict:
    """The same discrimination check `bench validate` runs, before the child does."""
    return validate_mod._review(Path(repo), task)  # noqa: SLF001 - shared rule, one source


def run_review_task(task: Task, *, repo: Path, reviewer: Reviewer,
                    clock: Callable[[], float] = time.monotonic) -> ReviewResult:
    started = clock()
    if task.kind != "review":
        return ReviewResult(task.id, "harness_error", "not a review task")
    verdict = preflight(repo, task)
    if not verdict.ok:
        return ReviewResult(task.id, "harness_error", f"preflight {verdict.line()}"[:200],
                            wall_seconds=clock() - started)
    try:
        outcome = reviewer(task)
    except Exception as exc:  # noqa: BLE001 - a plumbing failure is an outcome, not a crash
        return ReviewResult(task.id, "harness_error", str(exc)[:200], wall_seconds=clock() - started)
    if outcome not in REVIEW_VERDICTS + REVIEW_NON_VERDICTS:
        return ReviewResult(task.id, "harness_error", f"unknown reviewer outcome {outcome!r}",
                            wall_seconds=clock() - started)
    return ReviewResult(task.id, outcome, wall_seconds=clock() - started)


def to_outcomes(tasks: list[Task], results: list[ReviewResult]) -> list[TaskOutcome]:
    by_id = {t.id: t for t in tasks}
    return [TaskOutcome(r.task_id, "review", by_id[r.task_id].capabilities, r.outcome,
                        expect=by_id[r.task_id].expect, wall_seconds=r.wall_seconds)
            for r in results]


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(path), *args], check=False, capture_output=True, text=True)


def apply_mutation(repo: Path, base_ref: str, name: str, mutation: dict) -> str | None:
    """Commit `mutation` (`{"path", "old", "new"}`: replace the one occurrence of `old`) on
    top of `base_ref` and return the commit sha, or None when it cannot be applied mechanically."""
    path, old, new = (mutation or {}).get("path"), (mutation or {}).get("old"), (mutation or {}).get("new")
    if not all(isinstance(v, str) for v in (path, old, new)) or not path or not old or old == new:
        return None
    if Path(path).is_absolute() or ".." in Path(path).parts:
        return None
    with tempfile.TemporaryDirectory(prefix="gbfleet-bench-mut-") as tmp:
        tree = Path(tmp) / "tree"
        if _git(repo, "worktree", "add", "--detach", str(tree), base_ref).returncode != 0:
            return None
        try:
            target = tree / path
            if not target.is_file():
                return None
            text = target.read_text(encoding="utf-8", errors="surrogateescape")
            if text.count(old) != 1:
                return None
            target.write_text(text.replace(old, new), encoding="utf-8", errors="surrogateescape")
            ident = ["-c", "user.name=gbfleet", "-c", "user.email=gbfleet@localhost"]
            if _git(tree, "add", "--", path).returncode != 0:
                return None
            if _git(tree, *ident, "commit", "-q", "-m", f"bench: planted defect {name}").returncode != 0:
                return None
            sha = _git(tree, "rev-parse", "HEAD").stdout.strip()
            # Keep the commit reachable once the throwaway worktree is gone.
            if _git(repo, "update-ref", f"{DEFECT_REF_PREFIX}/{name}", sha).returncode != 0:
                return None
            return sha
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree))


def propose_review_tasks(repo: Path, candidates: list[Candidate], mutations: dict[str, dict],
                         capability: str = "F2") -> tuple[list[dict], list[Dropped]]:
    """Review-task bodies for candidates whose item has a red sabotage `mutation` (keyed by
    item id). The mutation applied to the merge is the planted defect; the merge is the clean
    branch. One that can't be applied mechanically is dropped as `mutation_not_applicable`."""
    tasks: list[dict] = []
    dropped: list[Dropped] = []
    for c in candidates:
        if not c.sabotage or c.item not in mutations:
            continue
        sha = apply_mutation(Path(repo), c.merge_sha, f"{c.id}-defect", mutations[c.item])
        if sha is None:
            dropped.append(Dropped(c.merge_sha, MUTATION_NOT_APPLICABLE, c.title))
            continue
        base = {
            "kind": "review", "base_sha": c.base_sha, "title": c.title[:256],
            "description": c.title + (f" ({c.item})" if c.item else ""),
            "acceptance": [], "touchpoints": c.touchpoints, "capabilities": [capability],
            "size_band": "S", "check": {"command": c.command, "tests": c.tests},
        }
        tasks.append({**base, "id": f"{c.id}-defect", "expect": "bounce", "ref": sha})
        tasks.append({**base, "id": f"{c.id}-clean", "expect": "sign_off", "ref": c.merge_sha})
    return tasks, dropped


# --- a review task on a live run (GRPH-1874) -------------------------------------------------

#: The pull-request url a bench review item names. The server's `open_run` refuses a review
#: task without one, because `claim_review` withholds a branch with no linked PR
#: (`pr_unlinked`). A seeded-defect ref lives in a local fixture repo and has no PR, so the run
#: names one under `.invalid` (RFC 2606): it matches the PR shape and can never resolve.
BENCH_PR_URL = "https://bench.gbfleet.invalid/{suite}/pull/{task}"

#: The note `sign_off` leaves on an item when a gate refuses `done` (`items.record_refusal`).
REFUSED_AT_DONE = "refused at `done` ("


def bench_pr(suite_id: str, task_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in f"{suite_id}") or "suite"
    return BENCH_PR_URL.format(suite=safe, task="".join(
        c if c.isalnum() or c in "-_." else "-" for c in task_id))


def review_description(task: Task) -> str:
    """The scratch item's brief: the task's own words plus where the change is."""
    where = (f"The change under review is `{task.ref}` in your worktree's repository. Read it "
             f"with `git diff {task.base_sha}..{task.ref}`"
             + (f" (it touches {', '.join(task.touchpoints)})" if task.touchpoints else "")
             + ". Sign off if it is correct; bounce with a reason if it is not.")
    return f"{task.description.strip()}\n\n{where}" if task.description.strip() else where


def verdict_of(item: dict) -> tuple[str, str]:
    """The reviewer's verdict on one scratch item, as (outcome, detail).

    `done` is a sign_off. A `bounce` sends the item to `next` and CLEARS `reviewed_by`, so
    the bounce is read off `bounce_reason`, never off `reviewed_by`. An item still in review
    with a refused `done` is a sign_off a server gate refused. The bench's PR is synthetic and
    has no CI, so the PR gates refuse every bench sign_off. That refusal is the harness's
    doing, not the reviewer's: the verdict was sign_off, and the refusal rides in `detail`.
    Anything else is `no_verdict`.
    """
    status = str(item.get("status") or "")
    if status == "done":
        return "sign_off", ""
    if status != "review" and str(item.get("bounce_reason") or "").strip():
        return "bounce", ""
    refusals = [str(e.get("detail") or "") for e in item.get("evidence") or []
                if isinstance(e, dict) and str(e.get("detail") or "").startswith(REFUSED_AT_DONE)]
    if status == "review" and refusals:
        return "sign_off", f"sign_off refused by the server: {refusals[-1][:160]}"
    return "no_verdict", f"item left in {status or 'an unknown state'} with no verdict"
