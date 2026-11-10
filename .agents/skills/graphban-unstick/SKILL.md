---
name: graphban-unstick
description: >
  Name why a gbfleet drain is stuck and say what clears it. Use when `until`
  exits review-unsigned, expires with nothing claimed, a child goes quiet, a
  lease drops, or WAVE_IDLE shows while items are still next. Or when the user
  runs /graphban-unstick.
user-invocable: true
---

# Unstick a drain

Name the item first. A stuck drain is one item, one holder, one timer. Run `fleet_status` and `get_item_details` on it before you do anything else.

## Which case

- `review-unsigned` — `until` exits because its review search is not limited to `--prd`. Say which item. A review row outside `--prd` is another PRD's work, not this wave's failure. A review claim has no TTL, so it stays until its reviewer signs off or releases it.
- Post-bounce pin — the bounced item stays pinned to its builder until the pin lapses. Wait. Do not reassign it.
- Dropped lease — a lease that lapsed is a timer. The item requeues when it runs out. Do not re-claim it for the child.
- Salvage note with no pull URL — the branch is the work. Find the branch and leave it. Do not delete or rebuild it.
- Live child with empty holdings — the child is running and holds nothing. That is not an idle wave, and `WAVE_IDLE` does not mean done while it keeps the drain open. Report it. Do not fix it from `watch-wave`.
- `WAVE_IDLE` while items are still `next` — a collision or a pin holds them. Name what holds them.

## Do not

Do not start a second `until` on the same repo. It mints another child onto the same cluster. Check `gbfleet service status --name <prd_id>` and leave the running one.

Do not `sign_off` the item. You did not review it.

Tell the user the item, the holder, and what clears it. Then stop.
