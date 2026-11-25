"""The CI gate has to actually gate on everything CI runs.

`.github/workflows/ci.yml` ends in a single `ci` job whose only purpose is to give
branch protection one name to point at. That works exactly as long as every real job
is in its `needs` list — and a job that is missing from it does not fail, does not
warn, and does not show up anywhere a reviewer looks. It runs, it burns the minutes,
and its result is discarded. The green tick means less than it did and nothing says so.

This file is in the backend suite because the backend paths-filter already includes
`.github/workflows/ci.yml`, so any edit to the workflow runs it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The gate itself, and the filter job the gate deliberately includes. Everything else
# defined in the workflow is a suite whose result has to reach the gate.
GATE = "ci"

# Jobs the gate must NOT cover, each for a stated reason. `security-scans` (GRPH-P57 D2) gates
# only the fleet merge into the release ref through its `scans_clean` attestation; in
# `ci.needs` it would block every feature PR at the forge. Named, so adding to this set is a
# visible decision rather than a quiet widening of the guard below.
NOT_GATED = {"security-scans"}

# Jobs that are not suites: the filter, the gate, the pure-AST refusal ratchet and the
# scan/attestation job. Everything else installs something and runs tests, so everything
# else has to bound itself. Named, like NOT_GATED, so widening it is a visible decision —
# and a NEW job defaults to being a suite, which is the direction that fails safe.
NOT_SUITES = {"changes", "ci", "refusal-ratchet", "security-scans"}

# GitHub's own default is 360 minutes, and longer on a self-hosted runner. A bound at that
# ceiling is the default wearing a costume, so the guard refuses it: the incident this
# exists for (GRPH-1928) was a shard that sat in `pytest -q -n auto` for over two hours.
MAX_TIMEOUT_MINUTES = 60


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_every_suite_job_bounds_itself():
    """GRPH-1928: a hung pytest held a drain for hours because no job set a timeout.

    CI run 45505794524 (PR #2272) left `Backend tests (Postgres + pgvector) (1/3)` in
    `pytest -q -n auto` from 22:59Z until a person cancelled it at 01:14Z. The other two
    shards had finished in minutes. Nothing in the workflow bounded the job, so the only
    limit was GitHub's default, and the drain read the whole wait as
    `review_waits_on_ci ... in_progress` — neither red nor green, just still going.

    Each job's number is ~2x its measured duration with the measurement in a comment; this
    guard is what keeps a new suite job from arriving without one.

    Sabotage: delete `timeout-minutes` from any suite job, or set one to 360.
    """
    jobs = _workflow()["jobs"]
    suites = set(jobs) - NOT_SUITES
    assert suites, "no suite jobs left — this guard would assert nothing"
    # An exemption that names a job that no longer exists hides a rename.
    assert NOT_SUITES <= set(jobs), f"NOT_SUITES names missing job(s): {sorted(NOT_SUITES - set(jobs))}"

    unbounded = sorted(n for n in suites if not jobs[n].get("timeout-minutes"))
    assert not unbounded, (
        f"job(s) {unbounded} run tests with no `timeout-minutes`, so a hang is bounded only "
        f"by GitHub's 360-minute default (longer on a self-hosted runner) and the drain waits "
        f"the whole time (GRPH-1928). Set one at ~2x the job's measured duration and cite "
        f"the measurement in a comment."
    )
    oversized = sorted(
        (n, jobs[n]["timeout-minutes"]) for n in suites
        if not isinstance(jobs[n]["timeout-minutes"], int)
        or not 1 <= jobs[n]["timeout-minutes"] <= MAX_TIMEOUT_MINUTES
    )
    assert not oversized, (
        f"job(s) {oversized} are bounded by something that is not an integer minute count in "
        f"1..{MAX_TIMEOUT_MINUTES}. A bound at GitHub's own 360-minute default is no bound."
    )


def test_ci_gate_covers_every_job():
    jobs = _workflow()["jobs"]
    assert GATE in jobs, f"no `{GATE}` job in {WORKFLOW}"
    # More than the gate itself, or this test asserts nothing.
    assert len(jobs) > 1

    needs = set(jobs[GATE]["needs"])
    ungated = set(jobs) - {GATE} - needs - NOT_GATED
    assert not ungated, (
        f"job(s) {sorted(ungated)} run in CI but are not in `{GATE}`.needs, so their "
        "result never reaches the required check. Add them."
    )
    assert not (needs & NOT_GATED), (
        f"{sorted(needs & NOT_GATED)} must stay out of `{GATE}`.needs (see NOT_GATED)")
    # Every exemption names a job that exists, or it exempts nothing and hides a rename.
    assert NOT_GATED <= set(jobs), f"NOT_GATED names missing job(s): {NOT_GATED - set(jobs)}"


def test_the_gate_job_runs_where_the_ledger_is():
    """Attestation posts to GRAPHBAN_URL from the runner.

    This repository's items live on ubuntu-srv. GitHub-hosted runners cannot reach
    that box, and cloud.graphban.dev is a different database — pointing there 404s
    every GRPH-* key (PR #451). The gate job has to run on the self-hosted runner
    that shares the box with the ledger.
    """
    runs_on = _workflow()["jobs"][GATE]["runs-on"]
    if isinstance(runs_on, str):
        runs_on = [runs_on]
    assert "graphban-ledger" in runs_on, (
        f"`{GATE}` runs-on {runs_on!r} — it will attest a Graphban that does not "
        "have this repository's items. The runner label is `graphban-ledger`."
    )


def test_the_filter_job_is_gated_too():
    """Named separately because it is the one entry someone would call redundant.

    `changes` computes what runs. If it fails, every suite skips, and `skipped` counts
    as a pass in the gate — so without `changes` in `needs`, a broken filter reports
    GREEN having run no tests at all, indistinguishable from a PR that needed none.
    """
    jobs = _workflow()["jobs"]
    assert "changes" in set(jobs[GATE]["needs"])


def test_every_job_declares_which_changes_it_needs():
    """A suite that never gates on the filter runs on every PR regardless — which is
    not wrong, but it is a decision, and the filters exist because someone decided the
    opposite. Absent an `if:`, that decision was made by omission."""
    jobs = _workflow()["jobs"]
    unconditional = [
        name
        for name, job in jobs.items()
        if name not in {GATE, "changes"} and "if" not in job
    ]
    assert not unconditional, (
        f"job(s) {sorted(unconditional)} have no `if:` guard, so they run on every PR. "
        "If that is intended, say so with `if: always()` rather than by leaving it out."
    )


def test_windows_job_gates_fleet_and_cli_on_windows_latest():
    """GRPH-855: Ubuntu-only CI let POSIX-only fleet breaks stay green (GRPH-588)."""
    job = _workflow()["jobs"]["windows"]
    assert job["runs-on"] == "windows-latest"
    guard = job["if"]
    assert "fleet" in guard and "cli" in guard, (
        f"windows job if: {guard!r} — a fleet- or cli-only PR would not pay for it, "
        "which is the opposite of a Windows gate"
    )


def test_windows_pytest_is_not_an_interactive_shell_wrapper():
    """The job this item adds must not die to the wrapper that runs pytest.

    First tip of PR #759 used the default Windows shell (pwsh). A child `stop()` sends
    CTRL_BREAK; pwsh treated it as a break-into-debugger (`Entering debug mode`) and
    the step exited 1.

    The salvage switched to `shell: cmd` plus `< NUL`. That stopped the debugger, then
    cmd.exe's batch wrapper prompted `Terminate batch job (Y/N)?` and the job exited
    STATUS_CONTROL_C_EXIT (-1073741510) — the bounce.

    bash on the Windows runner does neither. Stdin from `/dev/null` so a leftover
    prompt cannot hang the runner. Sabotage: put `shell: cmd` back on a pytest step.
    """
    job = _workflow()["jobs"]["windows"]
    pytest_steps = [
        step for step in job["steps"] if "pytest" in str(step.get("run", ""))
    ]
    assert pytest_steps, "windows job never invokes pytest"
    for step in pytest_steps:
        shell = step.get("shell")
        assert shell == "bash", (
            f"step {step.get('name')!r} uses shell: {shell!r}. pwsh enters its "
            "debugger on CTRL_BREAK; cmd.exe prompts Terminate batch job (Y/N)? — "
            "both killed PR #759. bash does not."
        )
        assert "</dev/null" in step["run"], (
            f"step {step.get('name')!r} leaves pytest stdin attached — a TTY or a "
            "cmd.exe Y/N prompt can hang the runner. Redirect from /dev/null."
        )


def test_the_canvas_bench_does_not_apt_install():
    """GRPH-1514: the bench step must not ask apt for Chromium's libraries.

    From 2026-11-11 the frontend job's `playwright install --with-deps chromium`
    failed on every PR. GitHub's ubuntu-26.04 image (ubuntu26/20261101.186)
    could not resolve archive.ubuntu.com or security.ubuntu.com, and the
    install exited 100 (run 43047053446, attempt 1). The libraries are
    installed once on the graphban-ci hosts, so the step only fetches the
    browser. Putting `--with-deps` back, or moving this job to ubuntu-latest,
    is the failure.
    """
    job = _workflow()["jobs"]["frontend"]
    runs_on = job["runs-on"]
    if isinstance(runs_on, str):
        runs_on = [runs_on]
    assert "graphban-ci" in runs_on, (
        f"frontend runs-on {runs_on!r} — ubuntu-latest is the image whose apt "
        "mirrors the bench could not resolve"
    )
    assert "ubuntu-latest" not in runs_on
    step = next(
        (s for s in job["steps"] if s.get("name") == "Canvas perf bench"),
        None,
    )
    assert step is not None, "frontend job has no Canvas perf bench step"
    script = step["run"]
    assert "pnpm exec playwright install chromium" in script, script
    assert "--with-deps" not in script, script
    assert "pnpm bench" in script, script


def test_no_ci_step_pushes():
    """CI checks; it does not write to the repo (GRPH-1185).

    A step that committed refusal coverage and ran `git push origin HEAD:main` was refused by
    main's branch protection and turned main red on every merge. Coverage is built into the
    package instead (`scripts/graphban_pack.py`), so no step in this workflow pushes."""
    steps = [(name, step) for name, job in _workflow()["jobs"].items()
             for step in job.get("steps", [])]
    assert steps
    pushing = [f"{name}: {step.get('name') or step['run'][:40]}" for name, step in steps
               if "git push" in (step.get("run") or "")]
    assert pushing == [], pushing
