"""Mechanical scoring of bench outcomes (GRPH-P98 section 5 and 8). Pure: no I/O.

A per-task outcome record goes in; the run score and per-capability rollups come out.
Plumbing outcomes (`never_registered`, `harness_error`) are counted on the run and
excluded from both the numerator and the denominator.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from gbfleet.bench.suite import CAPABILITY_LEAVES

BUILD_OUTCOMES = (
    "never_registered", "no_branch", "check_red", "check_green", "timed_out", "harness_error",
)
#: A review outcome is a verdict, or one of these non-verdict outcomes.
REVIEW_VERDICTS = ("bounce", "sign_off")
REVIEW_NON_VERDICTS = ("never_registered", "harness_error", "timed_out", "no_verdict")
PLUMBING = ("never_registered", "harness_error")
#: PRD-41's floor: a cell under this many scored tasks is grey.
FLOOR = 5
#: Leaves the shipped template suite claims (PRD-98 section 2). Others need the repo suite.
TEMPLATE_LEAVES = frozenset({"A1", "A3", "A5", "C2", "C1", "E1", "E2", "H5", "F1", "F2"})
NOT_REPORTED = "not reported"


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    kind: str  # "build" | "review"
    capabilities: tuple[str, ...]
    outcome: str
    #: review only: the task's expected verdict.
    expect: str = ""
    #: None means the vendor reported none. Never defaulted to 0.
    tokens: int | None = None
    wall_seconds: float | None = None
    #: build only: leaf -> (verdict, reason), verdict pass | fail | excluded. None means the
    #: record carries none and every leaf is scored on the check.
    leaves: dict[str, tuple[str, str]] | None = None


@dataclass(frozen=True)
class Cell:
    key: str
    n: int
    below_floor: bool
    #: share correct (or precision / recall for F1 / F2); None when n is 0.
    value: float | None
    family: bool = False


@dataclass(frozen=True)
class NotCovered:
    leaf: str
    suite: str  # the suite that could cover it


@dataclass(frozen=True)
class RunScore:
    n: int
    excluded: int
    green: int
    #: share of scored build tasks at check_green; None when n is 0.
    score: float | None
    f1: float | None
    f1_n: int
    f2: float | None
    f2_n: int
    cells: tuple[Cell, ...] = ()
    not_covered: tuple[NotCovered, ...] = ()
    tokens: int | None = None
    wall_seconds: float | None = None
    by_outcome: dict[str, int] = field(default_factory=dict)


def render_reported(value: int | float | None) -> str:
    """A vendor-reported figure, or "not reported". Zero is a figure, None is not."""
    return NOT_REPORTED if value is None else str(value)


def _check(rec: TaskOutcome) -> None:
    if rec.kind == "build":
        if rec.outcome not in BUILD_OUTCOMES:
            raise ValueError(f"task {rec.task_id!r}: unknown build outcome {rec.outcome!r}")
    elif rec.kind == "review":
        if rec.outcome not in REVIEW_VERDICTS + REVIEW_NON_VERDICTS:
            raise ValueError(f"task {rec.task_id!r}: unknown review outcome {rec.outcome!r}")
        if rec.expect not in REVIEW_VERDICTS:
            raise ValueError(f"task {rec.task_id!r}: review task needs expect bounce or sign_off")
    else:
        raise ValueError(f"task {rec.task_id!r}: unknown kind {rec.kind!r}")


def _correct(rec: TaskOutcome, leaf: str | None = None) -> bool:
    if rec.kind != "build":
        return rec.outcome == rec.expect
    if rec.leaves is not None and leaf in rec.leaves:
        return rec.leaves[leaf][0] == "pass"
    return rec.outcome == "check_green"


def _leaf_scored(rec: TaskOutcome, leaf: str) -> bool:
    """A leaf whose predicate could not be evaluated is not in that leaf's n."""
    if rec.kind != "build" or rec.leaves is None:
        return True
    return leaf in rec.leaves and rec.leaves[leaf][0] != "excluded"


def _sum(values: Iterable[int | float | None]):
    got = [v for v in values if v is not None]
    return sum(got) if got else None


def _cell(key: str, n: int, hits: int, *, family: bool = False) -> Cell:
    return Cell(key, n, n < FLOOR, hits / n if n else None, family)


def score_run(
    outcomes: Iterable[TaskOutcome], suite_leaves: Iterable[str] | None = None
) -> RunScore:
    """Score a run. `suite_leaves` is the set of leaves the suite covers; it defaults to
    every leaf a scored-or-excluded task names."""
    recs = list(outcomes)
    for r in recs:
        _check(r)
    covered = set(suite_leaves) if suite_leaves is not None else {
        c for r in recs for c in r.capabilities
    }
    excluded = sum(1 for r in recs if r.outcome in PLUMBING)
    scored = [r for r in recs if r.outcome not in PLUMBING]
    builds = [r for r in scored if r.kind == "build"]
    green = sum(1 for r in builds if r.outcome == "check_green")

    reviews = [r for r in scored if r.kind == "review"]
    bounces = [r for r in reviews if r.outcome == "bounce"]
    planted = [r for r in reviews if r.expect == "bounce"]
    f1_hits = sum(1 for r in bounces if r.expect == "bounce")
    f2_hits = sum(1 for r in planted if r.outcome == "bounce")

    by_leaf: dict[str, list[TaskOutcome]] = defaultdict(list)
    for r in scored:
        for c in set(r.capabilities):
            by_leaf[c].append(r)

    cells: list[Cell] = []
    grey_families: set[str] = set()
    for leaf in CAPABILITY_LEAVES:
        if leaf not in covered:
            continue
        if leaf == "F1":
            cell = _cell("F1", len(bounces), f1_hits)
        elif leaf == "F2":
            cell = _cell("F2", len(planted), f2_hits)
        else:
            rs = [r for r in by_leaf.get(leaf, []) if _leaf_scored(r, leaf)]
            cell = _cell(leaf, len(rs), sum(_correct(r, leaf) for r in rs))
        cells.append(cell)
        if cell.below_floor:
            grey_families.add(leaf[0])
    for fam in sorted(grey_families):
        seen = {id(r): r for leaf, rs in by_leaf.items() if leaf[0] == fam for r in rs}
        rs = list(seen.values())
        cells.append(_cell(fam, len(rs), sum(_correct(r) for r in rs), family=True))

    not_covered = tuple(
        NotCovered(leaf, "template" if leaf in TEMPLATE_LEAVES else "repo")
        for leaf in CAPABILITY_LEAVES if leaf not in covered
    )
    by_outcome: dict[str, int] = defaultdict(int)
    for r in recs:
        by_outcome[r.outcome] += 1
    return RunScore(
        n=len(builds), excluded=excluded, green=green,
        score=green / len(builds) if builds else None,
        f1=f1_hits / len(bounces) if bounces else None, f1_n=len(bounces),
        f2=f2_hits / len(planted) if planted else None, f2_n=len(planted),
        cells=tuple(cells), not_covered=not_covered,
        tokens=_sum(r.tokens for r in recs), wall_seconds=_sum(r.wall_seconds for r in recs),
        by_outcome=dict(by_outcome),
    )
