"""What a worker actually changed, measured off the worktree it changed it in.

PRD-22 S5 / P30 D10. One worker, one worktree, one branch means the diff boundary is
already exact: everything on that branch since it was cut is this worker's doing and
nothing else is.

**This module measures and reports. It does not patch the item.** The supervisor's
allowlist is still two reads (PRD-22 §4 / P30 G5). Write-back is done by whoever has
standing — `gbagent` during the run, `until` (planner) after a reap — via
`gbfleet.record.measured`. The server unions, so the client sends this reap's measured
paths only. Empty is reported as `touched: []` on the child record and is not a write:
wiping declared paths would read as "no collision".

Walk step 17 still has both operands: the child record's `touched` is the measurement,
the item's stored prediction stays (unioned with later measurements). The comparison
that overwrite would have collapsed is why the server unions rather than replaces.
"""

from __future__ import annotations

import subprocess

from .worktree import Worktree, is_seat_relative


def measure(tree: Worktree) -> list[str]:
    """Files this worker changed, from the commit its worktree was cut from.

    Read from the BRANCH rather than the working directory, so it still answers after
    the worktree has been reaped — which is when the answer is wanted, and after salvage
    has committed whatever was left uncommitted.

    Seat files are excluded. They are the supervisor's own doing, they are credentials,
    and reporting them as work a worker touched would be wrong twice over.
    """
    if not tree.base:
        # No fixed point, so no honest measurement. Returning [] here would read as "this
        # worker changed nothing", which is a claim rather than an absence of one.
        raise ValueError(
            f"{tree.branch} has no recorded base commit, so its diff cannot be measured "
            "against anything"
        )

    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{tree.base}..{tree.branch}"],
        cwd=str(tree.repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ValueError(f"could not diff {tree.branch}: {proc.stderr.strip()}")

    changed = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return sorted(f for f in changed if not is_seat_relative(f))
