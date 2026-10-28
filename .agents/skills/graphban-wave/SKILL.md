---
name: graphban-wave
description: >
  Drain one grilled PRD with gbfleet until: build, review, and merge until
  that PRD has nothing ready. Use when running a wave, spawning a wave for a
  PRD, looping until the PRD is done, or when the user runs /graphban-wave.
  One item is /graphban-delegation. Decompose is /graphban-prd.
user-invocable: true
---

# Drain a PRD

One command runs the cycle. Do not spawn the items. Do not stay in the loop.

## Before

`prd_coverage` for this PRD.

`gaps` or `shaped: false` — stop. Filing the tasks is `graphban-prd`. Do not `decompose_prd` here. An idle drain is not a finished PRD.

Coverage has tasks — continue.

## Dry run

Same flags as the real run, plus `--dry-run`. It mints nothing.

```bash
gbfleet until --repo . --server <url> --project <id> --adapter <vendor> \
  --prd <prd_id> --merge \
  --tier cheap=<adapter[:model]> --tier frontier=<adapter[:model]> \
  --dry-run
```

`--prd` is required. Without it the loop drains every ready item in the project. `backlog` is claimable, so parking work there does not scope the wave. `--prd` refuses a pre-minted seats file.

Read the dry run before installing. Held clusters name who holds them. `predicted: true` is a lower-confidence grouping. A plan for the wrong PRD is a stop.

A backend change whose acceptance is the Postgres suite needs `--allow psql` on both the dry run and the install.

## Run

Drop `--dry-run` and hand the process to the machine. This session ending must not kill it.

```bash
gbfleet service install --name <prd_id> -- --repo . --server <url> --project <id> \
  --adapter <vendor> --prd <prd_id> --merge \
  --tier cheap=<adapter[:model]> --tier frontier=<adapter[:model]>
```

`--tier` is what a tier means on this machine. Omit a tier the Fleet tier map already pins. `--request cheap` or `--request frontier` forces every delegation to that tier. The default follows each item's brief. Do not pick a model per item.

A service already installed under that name: `gbfleet service status --name <prd_id>` and leave it. A second `until` on the same repo mints another child onto the same cluster.

`gbfleet service status` prints the PATH the unit captured. An empty PATH is a drain that will not find the adapter. The API key is the one already stored for this repo. Do not print it, and do not pass a new one.

## While it runs

Status is `gbfleet service status --name <prd_id>`. The ledger is `fleet_status`. Do not attach a spawn watcher, and do not spawn a replacement.

`until` is idle when that PRD has nothing ready, nothing unsigned, and no live lease. `WAVE_IDLE` while items are still `next`, or held in review, is not done.

`review-unsigned` can be a review row outside this PRD. Say which item. Do not start another drain over it, and do not `sign_off` it yourself.

`--merge` lands the pull requests this wave opened. Leave every other open pull request. A salvage branch can be the only copy of the work.
