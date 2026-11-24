"""`gbfleet bench run`: one baseline run of an adapter against a bench suite (GRPH-P98 section 4).

Per build task: a worktree at `base_sha` with the check's test files held out, a child
spawned through the ordinary supervisor path (`supervisor.start_one`, so the registration
window, wall clock, reap and salvage are the ones natural children get), the check run
with the held-out files restored, and one outcome recorded and posted.

Review tasks (GRPH-P98 section 8) are run here too: after `review.preflight`, a reviewer child
is spawned with the run's adapter in a worktree at the task's `ref` (the planted-defect or clean
commit), and its verdict (`bounce`, `sign_off`) or non-verdict is the task's outcome, scored
against `expect`.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import validate as validate_mod
from .score import TaskOutcome
from .suite import Check, Suite, SuiteError, Task

#: Rough tokens a task costs, by `size_band`. An estimate for `--dry-run` and the cap
#: check only; real spend is whatever the vendor reports.
TOKENS_BY_BAND = {"S": 60_000, "M": 180_000, "L": 450_000}
DEFAULT_BAND_TOKENS = TOKENS_BY_BAND["M"]
REVIEW_POLL_SECONDS = 10.0  # how often a running reviewer child is checked for a recorded verdict
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

    path = suite_mod.template_manifest()
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
    """Keep build and review tasks; skip one whose estimate exceeds a `fleet_policy.caps` value, naming it."""
    plan = Plan(adapter, model, suite_id, suite_version, counts=dict(counts or {}))
    caps = caps or {}
    for t in tasks:
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
    #: leaf -> {"verdict": pass | fail | excluded, "reason": str}; None when not measured.
    leaves: dict | None = None


#: Files a build child must not leave in its diff (E2); the server's `_STATE_FILES`.
STATE_FILES = ("agents.md", "claude.md", ".gbfleet-instruction", ".gitignore", ".cursor/mcp.json",
               ".grok/config.toml", ".grok/mcp.json")
#: The B family (B1-B8) is what harvest assigns a repo-suite build task; its held-out check scores it.
B_LEAVES = tuple(f"B{i}" for i in range(1, 9))
CHECK_LEAVES = ("A1", "A3", "A5", "C2") + B_LEAVES
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.py$|\.(test|spec)\.(ts|tsx|js)$",
                        re.I)


#: Compiled artifacts a child's `git add -A` can sweep in; they are not its work (H5, C1).
_COMPILED = re.compile(r"(^|/)__pycache__/|\.py[co]$")
_SOURCE_TEST = re.compile(r"\.(py|ts|tsx|js)$")


def work_paths(paths: list[str]) -> list[str]:
    """`paths` without compiled artifacts (`__pycache__/`, `*.pyc`, `*.pyo`)."""
    return [p for p in paths if not _COMPILED.search(p)]


def _verdict(ok: bool | None, reason: str = "") -> dict:
    return {"verdict": "excluded" if ok is None else ("pass" if ok else "fail"), "reason": reason}


def diff_paths(repo: Path, branch: str, base: str) -> list[str] | None:
    done = _git(repo, "diff", "--name-only", f"{base}...{branch}")
    return None if done.returncode != 0 else [p for p in done.stdout.splitlines() if p]


NOT_COLLECTED = "child's tests are not collected by the project's runner"


def _run_tests(repo: Path, tree: Path, task: Task, tests: list[str]) -> tuple[bool | None, str]:
    """Run the child's own tests in `tree` the way the task's check runs: True green,
    False red, None when they could not be run (with the reason)."""
    argv, cwd = validate_mod.resolve_check(repo, tree, replace(task.check, tests=tuple(tests)))
    try:
        done = subprocess.run(argv, check=False, cwd=tree / cwd, capture_output=True, text=True,
                              timeout=validate_mod.CHECK_TIMEOUT_S, env=validate_mod.check_env(tree / cwd))
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, f"cannot run the child's test: {type(exc).__name__}"
    out = done.stdout + done.stderr
    if done.returncode != 0 and not re.search(r"usage:|ERROR: (file or directory|not found)|unrecognized arguments", out) \
            and re.search(r"Ran 0 tests|NO TESTS RAN|no tests ran", out):
        # The runner started and the child's files exist, but it collected nothing from them:
        # the child wrote a test the project never runs. That miss is the child's.
        return False, NOT_COLLECTED
    if done.returncode != 0 and re.search(
            r"usage:|ERROR: (file or directory|not found)|"
            r"collection error|errors during collection|"
            r"unrecognized arguments|Error: (Cannot find|No test)", done.stdout + done.stderr):
        # Not a test failure the child owns: the runner could not collect what it was handed.
        return None, "cannot collect the child's tests: " + (done.stderr or done.stdout).strip()[-120:]
    return done.returncode == 0, ""


def _c1(repo: Path, branch: str, task: Task, paths: list[str]) -> dict:
    """C1: the child's own test passes with its change and fails once its non-test change is
    reverted. A test that is red on the child's own branch measures nothing it wrote."""
    paths = work_paths(paths)
    tests = [p for p in paths if _TEST_PATH.search(p) and _SOURCE_TEST.search(p)]
    source = [p for p in paths if not _TEST_PATH.search(p)]
    if not tests or not source or task.check is None:
        return _verdict(None, "child changed no test or no non-test file")
    with tempfile.TemporaryDirectory(prefix="gbfleet-bench-c1-") as tmp:
        tree = Path(tmp) / "tree"
        if _git(repo, "worktree", "add", "--detach", str(tree), branch).returncode != 0:
            return _verdict(None, f"cannot check out {branch}")
        try:
            present = [t for t in tests if (tree / t).is_file()]
            if not present:
                return _verdict(None, "child's tests are all deletions")
            green, why = _run_tests(repo, tree, task, present)
            if green is None:
                return _verdict(None, why)
            if not green:
                return _verdict(False, why or "child's test fails with its change in place")
            for rel in source:
                if _git(tree, "cat-file", "-e", f"{task.base_sha}:{rel}").returncode == 0:
                    _git(tree, "checkout", task.base_sha, "--", rel)
                else:
                    (tree / rel).unlink(missing_ok=True)
            green, why = _run_tests(repo, tree, task, present)
            if green is None:
                return _verdict(None, why)
            return _verdict(not green, "" if not green
                            else "child's test still passes with its change reverted")
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree))


def leaf_verdicts(task: Task, repo: Path, branch: str, outcome: str, *, commits: bool = True) -> dict:
    """A verdict for every leaf the task claims, by that leaf's own predicate (GRPH-1877).

    `commits` is whether the child's branch carries any commit beyond the base. Without one
    there is no branch (E1 fails) and no diff to measure E2, H5 or C1 on (excluded).
    """
    from ..touchpoints import undeclared

    registered = outcome not in ("never_registered", "harness_error")
    if outcome == "no_branch":
        commits = False
    paths = diff_paths(repo, branch, task.base_sha) if registered and commits else None
    out: dict[str, dict] = {}
    for leaf in task.capabilities:
        if leaf in CHECK_LEAVES:
            ok = (outcome == "check_green") if outcome in ("check_green", "check_red", "no_branch") else None
            out[leaf] = _verdict(ok, "" if ok is not None else f"check not evaluated ({outcome})")
        elif leaf == "E1":
            out[leaf] = _verdict(commits if registered else None,
                                 "" if registered and commits else ("no branch" if registered else outcome))
        elif paths is None:
            out[leaf] = _verdict(None, f"no branch diff to measure ({outcome})")
        elif leaf == "E2":
            bad = [p for p in paths if p.lower() in STATE_FILES or p.lower().endswith(STATE_FILES)]
            out[leaf] = _verdict(not bad, ", ".join(bad[:5]))
        elif leaf == "H5":
            if not task.touchpoints:
                out[leaf] = _verdict(None, "task declares no touchpoints")
            else:
                bad = undeclared(work_paths(paths), list(task.touchpoints))
                out[leaf] = _verdict(not bad, ", ".join(bad[:5]))
        elif leaf == "C1":
            out[leaf] = _c1(repo, branch, task, paths)
        else:
            out[leaf] = _verdict(None, f"no predicate scores {leaf}")
    return out


Start = Callable[..., object]
#: `(task, scratch, item_id) -> (outcome, detail)`: a review item's verdict (`review.verdict_of`).
ReadVerdict = Callable[[Task, object, str], tuple[str, str]]

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


def template_fixture(workspace: Path) -> Path:
    """The template suite's fixture repo under `workspace`, built there on first use.

    Named by the template's content hash, so a version bump never reuses an older fixture;
    the generator is deterministic, so an existing one with its `root` tag is the same repo.
    """
    from . import suite as suite_mod
    from .template import fixture

    dest = Path(workspace) / f"template-fixture-{suite_mod.content_hash()[:12]}"
    if dest.is_dir() and _git(dest, "rev-parse", "-q", "--verify", f"refs/tags/{fixture.ROOT_REF}").returncode == 0:
        return dest
    return fixture.build(dest)


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
        # The check never ran (A-leaves excluded); E1/E2/H5/C1 are measured on what it pushed.
        return Result(task.id, "timed_out", "child ran past its wall clock", wall_seconds=wall,
                      leaves=leaf_verdicts(task, repo, tree.branch, "timed_out",
                                           commits=_commits_beyond(repo, tree.branch, task.base_sha) > 0))
    if _commits_beyond(repo, tree.branch, task.base_sha) == 0:
        return Result(task.id, "no_branch", wall_seconds=wall,
                      leaves=leaf_verdicts(task, repo, tree.branch, "no_branch"))
    verdict, why = validate_mod.run_check(repo, tree.branch, task.check, overlay=task.reference)
    if verdict == validate_mod.PASS:
        outcome, detail = "check_green", ""
    elif verdict == validate_mod.FAIL:
        outcome, detail = "check_red", ""
    elif verdict == validate_mod.TIMED_OUT:
        outcome, detail = "timed_out", why
    else:
        outcome, detail = "harness_error", f"{verdict}: {why}"
    return Result(task.id, outcome, detail, wall_seconds=wall,
                  leaves=leaf_verdicts(task, repo, tree.branch, outcome))


def _run_review(task: Task, code: str, *, repo: Path, scratch, start: Start, limits,
                clock: Callable[[], float], sleep: Callable[[float], None], cut: Callable[..., object],
                stop: Callable[..., object], reap: Callable[..., object],
                read_verdict: ReadVerdict | None, seat: dict, review_skip: tuple = ()) -> Result:
    """One review task: preflight, a reviewer child on the task's `ref`, its verdict read back.

    `seat` is the server's seat row. A review task must come back seated as review work, a
    `review_only` seat on an item already in `review`. A build seat would make the reviewer
    child claim the item as its builder, and no verdict could ever exist (GRPH-1878).
    `review_skip` names the run's other review items, so this child reviews its own.
    """
    from ..spawn import LaunchFailed
    from . import review as review_mod

    started = clock()
    try:
        pre = review_mod.preflight(repo, task)
    except Exception as exc:  # noqa: BLE001
        return Result(task.id, "harness_error", f"preflight: {str(exc)[:200]}")
    if not pre.ok:
        return Result(task.id, "harness_error", f"preflight {pre.line()}"[:200])
    if read_verdict is None:
        return Result(task.id, "harness_error", "no way to read a reviewer verdict")
    item_id = seat.get("item_id")
    if seat.get("kind") != "review" or not item_id:
        return Result(task.id, "harness_error",
                      "the server seated this review task as build work; a reviewer child "
                      "there would build it, not review it")
    try:
        # The task itself, not a copy: `repo_for` knows a template task by identity.
        tree = cut(task, base=task.ref)
    except Exception as exc:  # noqa: BLE001
        return Result(task.id, "harness_error", f"worktree: {str(exc)[:200]}")
    try:
        child = start(tree, code, task, scratch, review_skip=review_skip)
    except LaunchFailed as exc:
        reap(tree)
        return Result(task.id, "never_registered", str(exc)[:200], wall_seconds=clock() - started)
    except Exception as exc:  # noqa: BLE001
        reap(tree)
        return Result(task.id, "harness_error", str(exc)[:200], wall_seconds=clock() - started)
    timed_out = False
    polled: tuple[str, str] | None = None
    last_poll = started
    while child.running:
        now = clock()
        if now - started >= limits.child_wall_clock:
            stop(child)
            timed_out = True
            break
        if now - last_poll >= REVIEW_POLL_SECONDS:
            last_poll = now
            try:
                seen = read_verdict(task, scratch, item_id)
            except Exception:  # noqa: BLE001 - the final read below reports a real failure
                seen = None
            if seen and seen[0] in ("bounce", "sign_off"):
                # The verdict is recorded; the child may idle on a PR cooldown (GRPH-1899).
                stop(child)
                polled = seen
                break
        sleep(1.0)
    wall = clock() - started
    reap(tree)
    try:
        verdict, detail = polled if polled else read_verdict(task, scratch, item_id)
    except Exception as exc:  # noqa: BLE001
        return Result(task.id, "harness_error", f"verdict: {str(exc)[:200]}", wall_seconds=wall)
    if verdict in ("bounce", "sign_off"):
        # A verdict recorded before the wall clock ran out still counts.
        return Result(task.id, verdict, detail, wall_seconds=wall)
    if timed_out:
        return Result(task.id, "timed_out", "child ran past its wall clock", wall_seconds=wall)
    return Result(task.id, "no_verdict", detail, wall_seconds=wall)


def _commits_beyond(repo: Path, branch: str, base: str) -> int:
    done = _git(repo, "rev-list", "--count", f"{base}..{branch}")
    return int(done.stdout.strip() or 0) if done.returncode == 0 else 0


def _reseating(start: Start, reseat: Callable[[], str | None]) -> Start:
    """`start`, retried once on a fresh seat when the child reports its seat expired.

    A seat that aged out is the harness's fault, so a second failure is `harness_error`
    (excluded), never `never_registered` charged to the model (GRPH-1888).
    """
    from ..spawn import LaunchFailed

    def wrapped(tree, code, *args, **kwargs):
        try:
            return start(tree, code, *args, **kwargs)
        except LaunchFailed as exc:
            if "expired" not in str(exc):
                raise
            new = reseat()
            if not new:
                raise RuntimeError(f"seat expired and no fresh seat could be issued: {exc}") from exc
            try:
                return start(tree, new, *args, **kwargs)
            except LaunchFailed as again:
                if "expired" in str(again):
                    raise RuntimeError(f"seat expired twice: {again}") from again
                raise

    return wrapped


def run_plan(plan: Plan, *, repo: Path, client, open_scratch: Callable[[dict], object],
             start: Start, cut: Callable[..., object], limits, binary_version: str = "",
             clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             stop: Callable[..., object] = lambda child: None,
             reap: Callable[..., object] = lambda tree: None, wave: str = "bench",
             repo_for: Callable[[Task], Path] | None = None,
             read_verdict: ReadVerdict | None = None) -> dict:
    """Open the run, work each task, post the results. Returns the posted record.

    `repo_for(task)` names the repository a task's branch and check live in (the template's
    fixture for template tasks, the user's repo for repo-suite ones); without it, `repo`.

    `client.open_bench_run` and `client.post_bench_result` are the server's two bench routes;
    `open_scratch(run)` yields the client for the scratch project the children register in,
    built on `scratch_credential(run)`; a reply without that credential is `harness_error`.

    A review task goes to the server with its `ref`, `expect`, a brief naming the diff and a
    synthetic `pr` (`review.bench_pr`), so `open_run` puts it in `review` on a review-only
    seat. `read_verdict(task, scratch, item_id)` returns `(outcome, detail)` for its item.
    """
    from . import review as review_mod

    body = {"vendor": plan.adapter.split(":")[0], "model": plan.model, "binary_version": binary_version,
            "suite_id": plan.suite_id, "suite_version": plan.suite_version,
            "tasks": [{"id": p.task.id, "title": p.task.title, "description": p.task.description,
                       "touchpoints": list(p.task.touchpoints),
                       **({"kind": "review", "ref": p.task.ref, "expect": p.task.expect,
                           "description": review_mod.review_description(p.task),
                           "pr": review_mod.bench_pr(plan.suite_id, p.task.id)}
                          if p.task.kind == "review" else {})} for p in plan.tasks]}
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
        rows = {s["task_id"]: s for s in run.get("seats", [])}
        seats = {tid: s.get("enrolment_code") for tid, s in rows.items()}
        review_items = {tid: s["item_id"] for tid, s in rows.items()
                        if s.get("kind") == "review" and s.get("item_id")}
        scratch = open_scratch(run)

        def fresh_seat(task_id: str) -> str | None:
            """A seat minted as the task starts: the ones from open_run age out (GRPH-1888)."""
            try:
                return client.bench_task_seat(run_id, task_id).get("enrolment_code")
            except Exception:  # noqa: BLE001 - fall back to the seat from open_run
                return None

        for p in plan.tasks:
            code = fresh_seat(p.task.id) or seats.get(p.task.id)
            if not code:
                results.append(Result(p.task.id, "harness_error", "no seat issued for this task"))
                continue
            task_repo = repo_for(p.task) if repo_for else repo
            task_start = _reseating(start, lambda tid=p.task.id: fresh_seat(tid))
            if p.task.kind == "review":
                results.append(_run_review(p.task, code, repo=task_repo, scratch=scratch, start=task_start,
                                           limits=limits, clock=clock, sleep=sleep, cut=cut, stop=stop,
                                           reap=reap, read_verdict=read_verdict,
                                           seat=rows[p.task.id], review_skip=tuple(
                                               i for t, i in review_items.items() if t != p.task.id)))
                continue
            results.append(_run_task(p.task, code, repo=task_repo, scratch=scratch, start=task_start,
                                     limits=limits, wave=wave, clock=clock, sleep=sleep, cut=cut,
                                     stop=stop, reap=reap))
    by_id = {p.task.id: p.task for p in plan.tasks}
    post = {**{k: body[k] for k in ("vendor", "model", "binary_version", "suite_id", "suite_version")},
            "outcomes": [{"task_id": r.task_id, "outcome": r.outcome,
                          "capabilities": list(by_id[r.task_id].capabilities),
                          "tokens": r.tokens, "wall_seconds": r.wall_seconds,
                          **({"kind": "review", "expect": by_id[r.task_id].expect}
                             if by_id[r.task_id].kind == "review" else {}),
                          **({"leaves": r.leaves} if r.leaves is not None else {})}
                         for r in results]}
    if run_id:
        post["run_id"] = run_id
    if not results:
        return {"outcomes": [], "results": []}
    posted = client.post_bench_result(post)
    posted["results"] = results
    return posted


def to_outcomes(plan: Plan, results: list[Result]) -> list[TaskOutcome]:
    by_id = {p.task.id: p.task for p in plan.tasks}
    return [TaskOutcome(r.task_id, by_id[r.task_id].kind, by_id[r.task_id].capabilities, r.outcome,
                        expect=by_id[r.task_id].expect if by_id[r.task_id].kind == "review" else "",
                        tokens=r.tokens, wall_seconds=r.wall_seconds,
                        leaves=({k: (v["verdict"], v["reason"]) for k, v in r.leaves.items()}
                                if r.leaves is not None else None)) for r in results]
