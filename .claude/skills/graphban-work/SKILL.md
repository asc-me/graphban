---
name: graphban-work
description: >
  Claim and finish one Graphban item: register, claim_cluster or claim_next,
  heartbeat, evidence, release. Use when claiming work, implementing an item,
  updating item status, recording evidence, or when the user runs /graphban-work.
user-invocable: true
---

# Work an item

Take one item, leave a receipt, hand it to review. Reading the repo is `graphban-orient`: `search_code`, then `code_neighbors`, before grep or a wide read.

1. In a fleet, `register_agent` once before any claim. Read `active_role`, `heartbeat_interval_seconds`, and `tools_off_limits`. A name in `tools_off_limits` is still in the prompt; the server refuses the call. Heartbeat on that interval. Pass `agent_id` from this registration on later calls.
2. Fleet: `claim_cluster`. Solo: `claim_next`. `claimed: false` names who holds the area and when it frees. That is the answer. Take different work.
3. `get_item_details` once. The `brief` suggests `lane` and `tier` with a basis. You still choose both if you hand it to a child.
4. `heartbeat(id, status=<one line>, files=[the paths you are in])` on the lease interval. A heartbeat is not a `fleet_status` poll.
5. `update_item` as you go. Evidence appends. Touchpoints union: send the files you actually touched. An empty touchpoint list writes nothing. Status goes to `review`.
6. Stopping unfinished: `release_item`. A blocker that is still true tomorrow is status `blocked` with `blocker` set. `backlog` is claimable and will be handed out again. A build claim released without `to_status` goes back to `next`.

A product decision the item and its PRD framing do not already state: file a wait and stop. `create_item` with tag `wait:decision`, `status=blocked`, this item's `prd_id`, and a description that is the question plus what each answer would make you do. `link_items` with `a` this item, `b` the wait, `type` `dependency`, `reason` `wait:decision`. Leave this item `blocked`. Leave the wait blocked. Answering it is `graphban-answer`.

A broken build stays a free-text `blocker` on this item. That is not a wait.

Signing off is `graphban-review`. The builder does not set `done`.

Memory, when the next agent cannot see the lesson in the diff:

- `search_memory` first, as in `graphban-orient`.
- `add_memory`. Read the returned `status`. `candidate` does not appear in later search. A near-duplicate is rejected.
- `publish_memory` asks a judge. `kept: false` is a normal result.

Handing the item to a child is `graphban-delegation`. That is a `delegate`, not a second claim.
