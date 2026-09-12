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


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_ci_gate_covers_every_job():
    jobs = _workflow()["jobs"]
    assert GATE in jobs, f"no `{GATE}` job in {WORKFLOW}"
    # More than the gate itself, or this test asserts nothing.
    assert len(jobs) > 1

    needs = set(jobs[GATE]["needs"])
    ungated = set(jobs) - {GATE} - needs
    assert not ungated, (
        f"job(s) {sorted(ungated)} run in CI but are not in `{GATE}`.needs, so their "
        "result never reaches the required check. Add them."
    )


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
