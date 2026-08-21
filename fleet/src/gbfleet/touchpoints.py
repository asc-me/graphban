"""What a worker actually changed, measured off the worktree it changed it in.

PRD-22 S5. One worker, one worktree, one branch means the diff boundary is already
exact: everything on that branch since it was cut is this worker's doing and nothing
else is. Phase 1 of the AL-201 spike, which called it the highest-value lowest-risk
slice on its own.

**Measured and reported — deliberately NOT written back as `touchpoints`, which is what
S5 says to do.** Two reasons, and the second is the one that matters.

The first: §4 says the supervisor may not write items, and its outbound allowlist is two
reads. Widening it is a designed act, not a forbidden one, so this alone would only be an
argument to have.

The second is that writing the measurement into `touchpoints` **destroys the thing the
acceptance walk exists to check**. Walk step 17 asks to "read what it actually did —
which files each worker touched *versus what the cluster predicted*". `touchpoints` IS
the prediction: it is declared when the item is filed and it is what clustering divvies
work by. Overwrite it with the measurement and the comparison has one operand, forever,
and every future run looks perfectly predicted because prediction and outcome became the
same field.

So the supervisor measures, and reports. Whoever holds both halves — the planner, which
has the authority the supervisor deliberately does not — does the comparison and decides
what to record. That is D-j's shape applied to measurement rather than allocation: the
supervisor produces the fact, somebody with standing acts on it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .worktree import SEAT_FILES, Worktree


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
    return sorted(f for f in changed if f not in SEAT_FILES)
