"""`gbfleet bench run`: one baseline run of an adapter against a bench suite (GRPH-P98 section 4).

Per build task: a worktree at `base_sha` with the check's test files held out, a child
spawned through the ordinary supervisor path (`supervisor.start_one`, so the registration
window, wall clock, reap and salvage are the ones natural children get), the check run
with the held-out files restored, and one outcome recorded and posted.

Review tasks are not spawned here: they need a built branch and a second seat.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import validate as validate_mod
from .score import TaskOutcome
from .suite import Check, Suite, SuiteError, Task

#: Rough tokens a task costs, by `size_band`. An estimate for `--dry-run` and the cap
#: check only; real spend is whatever the vendor reports.
TOKENS_BY_BAND = {"S": 60_000, "M": 180_000, "L": 450_000}
DEFAULT_BAND_TOKENS = TOKENS_BY_BAND["M"]
#: The `fleet_policy.caps` keys that bound one task.
TASK_CAPS = ("per_attempt_tokens", "per_item_tokens")

EMPTY_REPO = "repo suite has 0 tasks; run `gbfleet bench harvest` or use `--suite template`"


def estimate_tokens(task: Task) -> int:
    return TOKENS_BY_BAND.get(task.size_band.strip().upper(), DEFAULT_BAND_TOKENS)


def task_from_row(row: dict) -> Task:
    """A server suite row (the section 1 manifest fields) as a Task."""
    check = row.get("check")
    return Task(
        id=str(row["id"]), kind=str(row.get("kind") or "build"), base_sha=str(row.get("base_sha") or ""),
        title=str(row.get("title") or ""), description=str(row.get("description") or ""),
        acceptance=tuple(row.get("acceptance") or ()), touchpoints=tuple(row.get("touchpoints") or ()),
        capabilities=tuple(row.get("capabilities") or ()), size_band=str(row.get("size_band") or ""),
        check=Check(str(check["command"]), tuple(check.get("tests") or ())) if check else None,
        reference=str(row.get("reference") or ""), expect=str(row.get("expect") or ""),
        ref=str(row.get("ref") or ""),
    )


def template_suite() -> Suite | None:
    """The shipped template suite, or None when this install carries none."""
    from . import suite as suite_mod

    path = Path(__file__).with_name("template_suite.toml")
    if not path.is_file():
        return None
    try:
        return suite_mod.load(path)
    except SuiteError:
        return None


@dataclass(frozen=True)
class Planned:
    task: Task
    tokens: int


@dataclass(frozen=True)
class Skipped:
    task_id: str
    reason: str


@dataclass
class Plan:
    adapter: str
    model: str
    suite_id: str
    suite_version: str
    tasks: list[Planned] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    #: Per source suite: how many tasks it contributed. `all` reports an empty one as 0.
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def estimated_tokens(self) -> int:
        return sum(p.tokens for p in self.tasks)

    def describe(self) -> list[str]:
        lines = [f"adapter {self.adapter}, model {self.model or 'vendor default'}, "
                 f"suite {self.suite_id} {self.suite_version}"]
        for name, n in self.counts.items():
            lines.append(f"{name} suite: {n} task{'s' if n != 1 else ''}")
        for p in self.tasks:
            lines.append(f"  {p.task.id} [{p.task.size_band}] ~{p.tokens} tokens")
        for s in self.skipped:
            lines.append(f"  skipped {s.task_id}: {s.reason}")
        lines.append(f"estimated spend ~{self.estimated_tokens} tokens over {len(self.tasks)} task(s)")
        return lines


def build_plan(tasks: list[Task], *, adapter: str, model: str, suite_id: str, suite_version: str,
               caps: dict | None = None, counts: dict[str, int] | None = None) -> Plan:
    """Keep build tasks; skip one whose estimate exceeds a `fleet_policy.caps` value, naming it."""
    plan = Plan(adapter, model, suite_id, suite_version, counts=dict(counts or {}))
    caps = caps or {}
    for t in tasks:
        if t.kind != "build":
            plan.skipped.append(Skipped(t.id, "review task: not run by `bench run`"))
            continue
        est = estimate_tokens(t)
        over = next(((k, caps[k]) for k in TASK_CAPS
                     if isinstance(caps.get(k), (int, float)) and est > caps[k]), None)
        if over:
            plan.skipped.append(Skipped(
                t.id, f"estimate {est} tokens exceeds fleet_policy.caps.{over[0]} {over[1]}"))
            continue
        plan.tasks.append(Planned(t, est))
    return plan


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(path), *args], check=False, capture_output=True, text=True)


def hold_out(tree: Path, task: Task) -> None:
    """Make the child's tree carry none of the check's test files.

    A test file the base lacks is removed; one the base has is put back at its base version.
    """
    for rel in task.check.tests if task.check else ():
        in_base = _git(tree, "cat-file", "-e", f"{task.base_sha}:{rel}").returncode == 0
        if in_base:
            _git(tree, "checkout", task.base_sha, "--", rel)
        else:
            (tree / rel).unlink(missing_ok=True)


@dataclass
class Result:
    task_id: str
    outcome: str
    detail: str = ""
    tokens: int | None = None
    wall_seconds: float | None = None


Start = Callable[..., object]

#: The field of the open-run reply carrying a credential scoped to the scratch project.
#: The children register there and the supervisor reads its roster there; the source
#: project's key cannot reach it (a project-scoped key reads and writes only its own
#: project), so a run without this credential has no seat a child could take.
SCRATCH_KEY = "api_key"
NO_SCRATCH_KEY = ("seat acquisition failed: the server issued no credential for the scratch "
                  "project (open-run reply has no `api_key`)")


def scratch_credential(run: dict) -> str | None:
    """The scratch project's key from an open-run reply, or None when it carries none."""
    key = run.get(SCRATCH_KEY) if isinstance(run, dict) else None
    return key if isinstance(key, str) and key.strip() else None


def _run_task(task: Task, code: str, *, repo: Path, scratch, start: Start, limits, wave: str,
              clock: Callable[[], float], sleep: Callable[[float], None], cut: Callable[..., object],
              stop: Callable[..., object], reap: Callable[..., object]) -> Result:
    from ..spawn import LaunchFailed

    started = clock()
    try:
        tree = cut(task)
    except Exception as exc:  # noqa: BLE001 - a plumbing failure is an outcome, not a crash
        return Result(task.id, "harness_error", f"worktree: {str(exc)[:200]}")
    hold_out(tree.path, task)
    try:
        child = start(tree, code, task, scratch)
    except LaunchFailed as exc:
        reap(tree)
        return Result(task.id, "never_registered", str(exc)[:200], wall_seconds=clock() - started)
    except Exception as exc:  # noqa: BLE001
        reap(tree)
        return Result(task.id, "harness_error", str(exc)[:200], wall_seconds=clock() - started)
    timed_out = False
    while child.running:
        if clock() - started >= limits.child_wall_clock:
            stop(child)
            timed_out = True
            break
        sleep(1.0)
    wall = clock() - started
    reap(tree)
    if timed_out:
        return Result(task.id, "timed_out", wall_seconds=wall)
    if _commits_beyond(repo, tree.branch, task.base_sha) == 0:
        return Result(task.id, "no_branch", wall_seconds=wall)
    verdict, why = validate_mod.run_check(repo, tree.branch, task.check, overlay=task.reference)
    if verdict == validate_mod.PASS:
        return Result(task.id, "check_green", wall_seconds=wall)
    if verdict == validate_mod.FAIL:
        return Result(task.id, "check_red", wall_seconds=wall)
    if verdict == validate_mod.TIMED_OUT:
        return Result(task.id, "timed_out", why, wall_seconds=wall)
    return Result(task.id, "harness_error", f"{verdict}: {why}", wall_seconds=wall)


def _commits_beyond(repo: Path, branch: str, base: str) -> int:
    done = _git(repo, "rev-list", "--count", f"{base}..{branch}")
    return int(done.stdout.strip() or 0) if done.returncode == 0 else 0


def run_plan(plan: Plan, *, repo: Path, client, open_scratch: Callable[[dict], object],
             start: Start, cut: Callable[..., object], limits, binary_version: str = "",
             clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             stop: Callable[..., object] = lambda child: None,
             reap: Callable[..., object] = lambda tree: None, wave: str = "bench") -> dict:
    """Open the run, work each task, post the results. Returns the posted record.

    `client.open_bench_run` and `client.post_bench_result` are the server's two bench routes;
    `open_scratch(run)` yields the client for the scratch project the children register in,
    built on `scratch_credential(run)`; a reply without that credential is `harness_error`.
    """
    body = {"vendor": plan.adapter.split(":")[0], "model": plan.model, "binary_version": binary_version,
            "suite_id": plan.suite_id, "suite_version": plan.suite_version,
            "tasks": [{"id": p.task.id, "title": p.task.title, "description": p.task.description,
                       "touchpoints": list(p.task.touchpoints)} for p in plan.tasks]}
    results: list[Result] = []
    run_id = None
    run: dict | None = None
    try:
        run = client.open_bench_run(body)
    except Exception as exc:  # noqa: BLE001 - no seats: every task is plumbing, not a miss
        results = [Result(p.task.id, "harness_error", f"seat acquisition failed: {str(exc)[:200]}")
                   for p in plan.tasks]
    else:
        run_id = run.get("id")
    if run is not None and scratch_credential(run) is None:
        # Never fall back to the source key: the child would fail to register in the scratch
        # project and the run would read as a fleet of no-shows instead of plumbing.
        results = [Result(p.task.id, "harness_error", NO_SCRATCH_KEY) for p in plan.tasks]
    elif run is not None:
        seats = {s["task_id"]: s["enrolment_code"] for s in run.get("seats", [])}
        scratch = open_scratch(run)
        for p in plan.tasks:
            code = seats.get(p.task.id)
            if not code:
                results.append(Result(p.task.id, "harness_error", "no seat issued for this task"))
                continue
            results.append(_run_task(p.task, code, repo=repo, scratch=scratch, start=start,
                                     limits=limits, wave=wave, clock=clock, sleep=sleep, cut=cut,
                                     stop=stop, reap=reap))
    by_id = {p.task.id: p.task for p in plan.tasks}
    post = {**{k: body[k] for k in ("vendor", "model", "binary_version", "suite_id", "suite_version")},
            "outcomes": [{"task_id": r.task_id, "outcome": r.outcome,
                          "capabilities": list(by_id[r.task_id].capabilities),
                          "tokens": r.tokens, "wall_seconds": r.wall_seconds} for r in results]}
    if run_id:
        post["run_id"] = run_id
    if not results:
        return {"outcomes": [], "results": []}
    posted = client.post_bench_result(post)
    posted["results"] = results
    return posted


def to_outcomes(plan: Plan, results: list[Result]) -> list[TaskOutcome]:
    by_id = {p.task.id: p.task for p in plan.tasks}
    return [TaskOutcome(r.task_id, "build", by_id[r.task_id].capabilities, r.outcome,
                        tokens=r.tokens, wall_seconds=r.wall_seconds) for r in results]
