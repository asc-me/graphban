"""Split the suite across CI runners by test FILE (2026-11-01).

`GRAPHBAN_TEST_SHARD=k/N` (1-based) keeps only the files packed into shard k of N. Unset,
everything runs: local runs and any job that does not ask are unaffected.

Packing is largest-first onto the least-loaded shard, by the per-file seconds in
`shard_weights.json` (measured setup+call in a SERIAL Postgres run — see REFRESHING below).
A file that is not in the weights yet gets the median weight, so a new test file still lands
in exactly one shard; refresh the weights when the split drifts out of balance. Every shard
computes the same partition from the same collected file list, which is what makes "exactly
one shard" hold without the shards talking to each other.

By file, not by test, so a module's fixtures are built once per run, not once per shard.

REFRESHING (GRPH-1903). Time one SERIAL Postgres process over a large selection of files,
summing `report.duration` where `report.when in ("setup", "call")`, and keep the median of
three passes. Two ways to get a number that looks measured and is not:

- Not `-n 4`. This docstring used to say "4 workers", and that was never the committed
  scale: three seats measured a small `-n 4` selection 2.4x-7.6x off the committed values
  and not consistently in one direction, so it is contention noise, not a scale.
- Not one file at a time. Session-scoped setup lands on whichever file pays it, so a file
  measured ALONE reads heavy: `tests/test_stale_blocker.py` is 5.91s alone and 2.13s inside
  a 35-file session. CI runs ~173 files per shard in one `-n auto` process, so the
  in-session figure is the CI-faithful one.

The committed numbers are not one scale. Against a 2026-11-24 refresh they run 0.56x-8.31x
(`tests/test_prd_coverage.py` 0.93 committed vs 7.73 measured; the three lowest ratios are
weights hand-committed during recent CI-red rebuilds, measured run-alone and so sitting
above the in-session scale). That costs balance, never correctness. But appending today's
measurements to the older ones is mixing scales, so when the split actually drifts,
re-measure the whole file in one pass instead of appending to it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from statistics import median

ENV = "GRAPHBAN_TEST_SHARD"
_WEIGHTS = Path(__file__).with_name("shard_weights.json")


def parse(value: str | None) -> tuple[int, int] | None:
    """`"2/3"` → (2, 3). Unset/blank → None (run everything). Anything else is an error:
    a typo must not quietly run zero tests and report green."""
    if not value or not value.strip():
        return None
    try:
        k, n = (int(x) for x in value.strip().split("/"))
    except ValueError:
        raise ValueError(f"{ENV} must look like k/N, got {value!r}") from None
    if not (n >= 1 and 1 <= k <= n):
        raise ValueError(f"{ENV}={value!r}: need 1 <= k <= N")
    return k, n


def load_weights() -> dict[str, float]:
    return json.loads(_WEIGHTS.read_text())


def partition(files: list[str], n: int, weights: dict[str, float]) -> list[list[str]]:
    """`files` split into `n` lists covering each file exactly once."""
    default = median(weights.values()) if weights else 1.0
    order = sorted(set(files), key=lambda f: (-weights.get(f, default), f))
    shards: list[list[str]] = [[] for _ in range(n)]
    load = [0.0] * n
    for f in order:
        i = min(range(n), key=lambda j: (load[j], j))
        shards[i].append(f)
        load[i] += weights.get(f, default)
    return shards


def select(items, value: str | None):
    """(kept, deselected) for the collected `items`, by their file."""
    spec = parse(value)
    if spec is None:
        return list(items), []
    k, n = spec
    files = [it.nodeid.split("::", 1)[0] for it in items]
    mine = set(partition(files, n, load_weights())[k - 1])
    kept = [it for it, f in zip(items, files) if f in mine]
    dropped = [it for it, f in zip(items, files) if f not in mine]
    return kept, dropped


def env_value() -> str | None:
    return os.environ.get(ENV)
