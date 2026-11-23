# gbfleet template suite

Version 1.0.0 (its own semver; bump it whenever `suite.toml`, `fixture.py` or this file changes,
then refresh `hash.toml`). The tasks run against a small fixture repo that `fixture.py` builds
deterministically at run time, so a bench run never needs your code.

16 tasks: 6 build tasks (`build-*`, base `root`, reference `mN-ref`) and 10 review tasks
(5 planted-defect `rN-defect` expecting `bounce`, 5 clean `rN-clean` expecting `sign_off`).

## Leaves covered

- A1, A3, A5, C2 — the check goes green (6 build tasks each).
- C1 — the child's own test fails when its non-test change is reverted (6).
- E1 — the child registered and reached a branch (6).
- E2 — no stray or instruction files (6).
- H5 — no edits outside the touchpoints (6).
- F1 — planted-defect review tasks (5).
- F2 — clean review tasks (5).

## Leaves not covered

A2, A4, B1–B8, H1–H4, C3, E3 and F3. These need the repo suite (`gbfleet bench harvest`).

`gbfleet bench validate` refuses this template if it claims a leaf with fewer than 5 tasks.
