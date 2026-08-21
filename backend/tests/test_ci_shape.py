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
