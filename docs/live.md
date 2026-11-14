# Live

**Live** shows who is on this project right now, what they hold, what they called, and whether a PR was recorded.

## What it shows

- **Agents, grouped by person**: filter by user; Unattributed is a real bucket for keys with no
  user. Offline agents are collapsed under headers with counts; one that still holds work
  (a holding, a lease, an unclaimed delegation) stays in view, faded.
- **Leases**: files are area leases, not writes; Graphban cannot see the agent's disk. An online
  agent holding work with no lease is unreserved, not idle. Predicted and off-map are labelled.
- **Recorded PRs**: shown only if a URL was stored on a holding. Graphban does not fetch git.
- **What it is doing**: every call an agent makes, reads included. Click a row for its timeline,
  or focus an agent to narrow the stream. Calls that named no agent are counted on the credential.
- **Delegations**: what a planner handed to children: open, claimed (by whom, declared model),
  expired (a spawn that died before registering), or superseded. A tier mismatch is stated.
- **Tickets in motion** (lanes over the last ten minutes) and the **planner desk** (open
  allocations and anything waiting on a colliding area).

## Reading an empty result

"No agents on this project yet" means none has registered. Labels are words, not blanks:
"unrecorded" PR, "no calls recorded", "no delegations", "no status reported", "no planner
online". Each is a measurement. A feed that fails to load is an error, not an empty feed.

## Related

[fleet.md](fleet.md), [code-graph.md](code-graph.md), [activity.md](activity.md), [delegation.md](delegation.md).
