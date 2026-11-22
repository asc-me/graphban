---
name: graphban-prove
description: >
  The finish bar before a builder moves an item to review: both database
  engines, sabotage of the caller, one read of the live surface. Use before
  update_item sets status=review, or when the user runs /graphban-prove.
user-invocable: true
---

# Prove it before review

A green unit suite is not the bar. Run this before `update_item` moves the item to `review`. `graphban-work` says evidence appends; this is the procedure that fills it.

1. Heartbeat immediately before any suite that can run longer than the lease, and again when that suite returns. A lease that lapses mid-suite requeues the item.
2. Run the SQLite suite. Then run the Postgres suite. Both database engines, in that order. A pass on one engine is not a pass. Record each as a `test` evidence entry with `passed`, `failed`, and `scope`. If the item's test scope is `agent_targeted`, run the targeted tests on both engines and leave the full suites to CI.
3. Break the caller on purpose: pass an empty argument, drop a field, or skip a branch. Run the test that should catch it. Record a `sabotage` receipt with `claim`, `mutation`, and `tests_failed`. `tests_failed` above zero means the test can fail. Zero means the test cannot fail, so fix the test and repeat. Revert the break before you commit.
4. Read the live surface the item names once: the endpoint, the page, the command output. Quote the sentence it returned in a `note` evidence entry. An empty list is a finding, not a pass: say so and do not hand it off as proven.
5. Commit, declare touchpoints, then set `review`.

The builder does not set `done`. Signing off is `graphban-review`.
