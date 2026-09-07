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


def including_stream(adapter: str, git_paths: list[str], stdout: str) -> list[str]:
    """Union git-diff with a vendor stream parser, if the adapter has one.

    Git-diff is ground truth (shell writes, not just tool calls). The stream is extra
    capture — Cursor writeToolCall (GRPH-215) — and empty extra is not a write.
    """
    from .adapters import ADAPTERS

    impl = ADAPTERS.get(adapter)
    extra = impl.stream_touched(stdout) if impl is not None else []
    if not extra:
        return list(git_paths)
    return sorted(set(git_paths) | set(extra))


def covers(area: str, path: str) -> bool:
    """Does a declared touch-area cover a measured path?

    **Deliberately BROAD, and that direction is the whole point.** This decides whether a
    file a worker changed was already declared; a narrow test would call covered files
    undeclared and the report would cry wolf. The server's `areas_collide` takes the union
    of a prefix rule and a parent-directory rule for the mirror-image reason, and this must
    stay at least as broad as that — anything flagged here is something the server would
    also have judged a collision.

    It is a client-side approximation and says so. It raises a QUESTION for a human, never
    a verdict: the server owns what "collides" means, and a second definition that decided
    anything would be a second definition of the rule.
    """
    import fnmatch
    import posixpath

    area = (area or "").strip().rstrip("/").lower()
    path = (path or "").strip().lower()
    if not area or not path:
        return False
    if area == path or path.startswith(area + "/"):
        return True
    if fnmatch.fnmatch(path, area):
        return True
    # A bare directory name (`backend`, `web/src`) covers everything under it, and a file
    # declared as its parent's sibling does not — that asymmetry is the server's too.
    return posixpath.dirname(path) == area


def undeclared(measured: list[str], declared: list[str]) -> list[str]:
    """Measured paths that no declared touch-area covers (GRPH-785).

    The partition that keeps two workers off the same file is computed from `touchpoints`.
    When a worker changes a file nobody declared, the partition's INPUT was wrong — and
    nothing noticed, because the server unions measured paths into the declaration and the
    two become indistinguishable the moment they are stored.

    An item that declared NOTHING is not drift: its areas were predicted server-side, which
    is a known-lower-confidence state the board already marks, and reporting every file it
    touched would bury the real cases.
    """
    if not declared:
        return []
    return sorted(p for p in (measured or []) if not any(covers(a, p) for a in declared))


def overlaps(touched_by_branch: dict[str, list[str]]) -> dict[str, list[str]]:
    """Files changed on more than one branch in the same wave — path -> branches.

    **The check that needs no declarations to be right.** Exact paths, plain set
    intersection, no coverage rule and no second definition of anything: two workers
    changed the same file, which is the failure the partition exists to prevent, observed
    rather than predicted. If touchpoints were wrong this still fires; if they were right
    and the divvy was wrong this still fires.
    """
    seen: dict[str, list[str]] = {}
    for branch, paths in (touched_by_branch or {}).items():
        for path in set(paths or []):
            seen.setdefault(path, []).append(branch)
    return {path: sorted(bs) for path, bs in sorted(seen.items()) if len(bs) > 1}
