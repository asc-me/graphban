---
name: graphban-ship
description: >
  Open a pull request, rebase, or look at a salvage branch without guessing the
  base. Use when opening a pull request, rebasing a branch, proposing a
  change for review, or reading a salvage branch.
user-invocable: true
---

# Ship a branch

## Base

Call `get_context` and read `gitops`. Say which base rule applied:

1. the flag you were given,
2. a measured `base_branch`,
3. the remote default.

An unmeasured base is not `main`. When `gitops` reports the base as unmeasured, fall through to the remote default and say so. Do not guess.

## Subject

The subject is the item id, for example `GRPH-123: <title>`.

## Whitelist

The touchpoints snapshotted at wave start are the whitelist. A file outside them is why a proposal was withheld. Run `gbfleet check-touchpoints`, then declare the file with `update_item` or take it out of the diff. A branch that is behind the base is also withheld: rebase it first.

## Salvage

A salvage is a pushed branch and a note, with no pull URL. A salvage HEAD stays a branch: do not propose its commit as a pull request. A salvage pull request stays open when it is the only copy of the work. A salvage SHA is not signed off while CI is red.

## Done

Ledger `done` is not merged. Check the pull request state before saying the work landed.
