# PRD-36 — Delegate to seat: a bound seat claims its item on registration, and spawn takes a tier

**Ledger id:** GRPH-P36
**Status:** approved — grill complete 2026-09-04; answers absorbed into D13–D19, the revised D3/D6/D12, and §9. v1.0.
**Depends on:** PRD-35 (the delegation record, seat lineage, requested vs declared tier) · PRD-22 (the supervisor, D-b no-parent rule, `spawn`) · PRD-19 (seats, consumption, the planner-only mint gate) · PRD-24 S7 (gbagent `--item`)
**Complemented by:** the Live board (where the delegation reads `claimed` at spawn) · `gbfleet until` (which stops predicting) · the generated planner prompts
**Touches:** `backend/app/models/__init__.py` (`Enrolment` +2 columns) · `backend/alembic/versions/0105_*` · `backend/app/services/fleet.py` (`issue_enrolment`, `mint_enrolment_as`, `register_agent`, `sign_off`, `claim_review`) · `backend/app/services/delegation.py` · `backend/app/mcp_server.py` (`delegate.seat`, `register_agent.assigned`) · `fleet/src/gbfleet/{seat,mcp,cli,until}.py` · `fleet/src/gbfleet/adapters/gbagent.py` · `AGENTS.md` · `scripts/gen_subagents.py` · `docs/mcp.md` · `docs/data-model.md`

---

## 1. Overview

<!-- framing -->

PRD-35 made a delegation a ledger fact: what was asked, what turned up, how it ended. It left the executor to the harness, and the harness it assumed was the Claude Code subagent tool. That tool has two limits on this fleet: it cannot spawn a model outside the Anthropic tiers, and on the operator's endpoint it has refused the very tier ids the delegation asks for. Meanwhile `gbfleet` can already run a child on any adapter, including a local model that costs nothing per token, and the ledger already links that child's claim to the delegation through the seat it registered on.

This PRD joins the two. **A delegation can mint the seat its child will run on, the seat carries the item, and registering on that seat claims the item.** `gbfleet spawn` takes a tier and resolves it to an adapter and model the operator named at launch. The parent keeps working; the outcome comes back through the ledger, as the item changing state on the board and the feed, never as a reply in the parent's context.

The load-bearing invariant, inherited and extended:

**The server records and arbitrates. The harness spawns what the operator named. The child claims what the seat carries, and only that. Nothing here lets the server choose an adapter, and nothing here lets a child choose its item.**

### 1.1 What this is not

- **Not a replacement for the divvy.** `claim_cluster` stays the way a free worker takes work. A bound seat is for work the parent already chose to hand over; an unbound seat behaves exactly as today.
- **Not a subagent.** The child is a separate process on its own seat, with no `parent_agent_id`, as PRD-22 D-b requires. Lineage is the seat (PRD-35 D7), not the call tree.
- **Not a chat return.** `spawn` returns the child's agent id when it registers, and nothing later. The parent reads the item.
- **Not a hook layer.** No per-tool guards, no transcript regexes, no `/tmp` state. What the pack under review enforced by regex, the ledger records and the Live board shows.

---

## 2. Problem

<!-- framing -->

Verified against the tree at `8ac3ac01`, 2026-09-04.

### 2.1 The delegation has no executor the fleet can name

`delegate` writes the request; the planner prompts say "spawn by lane and tier" and, for Claude Code, "pass `model: haiku` on the Agent call". On this endpoint that call has returned "Model not exist" for fresh subagents, and `fork` ignores the pin. The record then reads `requested cheap, declared frontier` on every delegation, which is honest and useless.

### 2.2 The fleet's own delegation is a prediction

`gbfleet until` delegates the seed of the next free cluster before minting the seat (PRD-35 D12), but the child claims through the divvy and may land on a different cluster. The PRD names this as a risk: the delegation then reads `expired, nothing claimed` while the child works something else. The seat is minted for an agent, never for an item (`issue_enrolment` takes `role` and `wave`; `Enrolment` has no item column).

### 2.3 There is no way to claim an item by id from a client

`claim_item` exists as a service; the MCP surface offers `claim_next`, `claim_cluster` and `next_cluster`, each of which chooses. gbagent's `--item` argument (PRD-24 S7) names an item to heartbeat, but nothing claims it for the child. A child told "build GRPH-51" cannot take GRPH-51.

### 2.4 The supervisor knows models, not tiers

`gbfleet spawn` takes `adapter` and `model`, named and never inferred. `until` takes `--tier` for the delegation it writes but still mints a seat with no model, and there is no table from tier to adapter. The parent that typed `tier: cheap` cannot say "and run it on the cheap thing".

### 2.5 The manifest has sixteen tokens of headroom

PRD-35 left the full manifest at 14184 against the 14200 ceiling. Any property this PRD adds to a Graphban tool is paid by a trim. Properties on `gbfleet`'s own MCP tools are free of that ceiling; they are not in the Graphban manifest.

---

## 3. Goals

1. A parent can hand one item to one child on a cheaper model with two calls, `delegate` and `spawn`, and keep working.
2. The child holds that item from the moment it registers, with area reservations, and cannot take anything else on that seat.
3. The tier a parent typed resolves to an adapter and model the operator named, never one the supervisor picked.
4. `gbfleet until` stops predicting: its delegations are bound seats, and the PRD-35 risk closes.
5. The outcome is read from the ledger. The parent's context grows by the size of two tool results, not by the child's transcript.
6. No new Graphban tool. No ceiling raise.

## 4. Non-Goals

- Choosing which items to delegate. The brief suggests, the parent decides (PRD-35 D5).
- Any change to review independence. A seat-lineage child is a separate process; whether its delegator may review its work is decided by the existing `independent()` rules, and §9 asks the grill whether that is right.
- Running the child inside the parent's process, or streaming its output to the parent.
- A tier table in the server. The mapping from tier to adapter is a launch flag on the supervisor, because the adapter is a property of the machine the supervisor runs on.
- Multiple items per seat. One seat, one item. A cluster is what `claim_cluster` is for.
- Any Claude Code hook.

---

## 5. Key decisions

| # | Decision | Why |
| --- | --- | --- |
| D1 | A seat may carry an item and the delegation that asked for it | `Enrolment.item_id` and `Enrolment.delegation_id`, both nullable. An unbound seat is today's seat. The seat is the executor binding: the one server-issued object that reaches the child by construction. |
| D2 | `delegate(..., seat=true)` mints the bound seat | One call, one row each in `delegations` and `enrolments`, the code returned once. Minting stays gated as it is today (`mint_enrolment_as`: planner, or all-in-one), so a worker cannot mint itself a child. No new tool; one boolean property. |
| D3 | Registering on a bound seat claims the item, server-side | `register_agent` with a bound seat runs `claim_item` for the new agent and reserves the item's areas the way `claim_cluster` does for a one-item cluster. The reply carries `assigned: {item, state}`. The child never calls a claim tool. |
| D4 | `assigned.state` has three values, and `taken` is the third | `claimed` (the child holds it), `taken` (someone else holds it or it is no longer claimable, with who), `none` (an unbound seat). A child that reads `taken` exits; the delegation was already superseded or finished by whoever took it, and the board says so. Registration is not refused: the child must exist to be told. |
| D5 | The claim at registration links the delegation at once | PRD-35 D7's seat lineage fires inside the same transaction: the delegation reads `claimed by <child>` before the child has made a second call. `declared_model` comes from the child's `capabilities`, as today. |
| D6 | `gbfleet spawn` takes `tier`, resolved through operator-named flags | `gbfleet mcp --tier cheap=gbagent:qwen3.6:35b-a3b-coding-mtp-det --tier frontier=claude:opus` (and the same on `until`). `spawn(tier=cheap)` is `spawn(adapter, model)` looked up in that table. The supervisor still chooses nothing: the operator named the mapping at launch. `adapter` and `model` remain accepted and override the tier. A tier with no mapping is refused, naming the flag. |
| D7 | The instruction names the item | A bound seat's instruction file says "you hold GRPH-X, assigned at registration; read it with `get_item_details`, build it, move it to review; do not call claim_cluster or claim_next". gbagent additionally receives `--item`. The reviewer instruction is unchanged. |
| D8 | The return channel is the ledger | `spawn` returns `{agent_id}` on registration, as today. The parent reads the item, the Live board or `fleet_status` when it wants to know. Nothing pushes a result into the parent. |
| D9 | `until` mints bound seats | The loop's delegation (PRD-35 D12) becomes `delegate(seat=true)` on the seed, then `spawn` on that seat. The divvy prediction goes away, and with it the risk table's first entry. A seed that is not claimable at registration reads `taken` and the loop moves on. |
| D10 | Delegate items, never fragments | AGENTS.md step 5 and the planner prompts say it: if it needs a seat, it needs an item with a title, touchpoints and acceptance. A lookup is done inline or filed. This is the pack's four-shape rule reduced to the one shape the ledger can describe. |
| D11 | A bound seat that expires unconsumed leaves the delegation to expire | No new state. The seat TTL is 30 minutes; the delegation lease is 600 seconds. Both already read as absence on the board. |
| D12 | Manifest cost paid by trims, ceiling unchanged | `delegate` gains `seat` and its result gains `enrolment_code`; `register_agent`'s result gains `assigned`. Measured and pinned in `test_mcp_footprint` and `test_tool_tiers`, with the trims named in the pinned-constant comments and permanent. A future addition fails the pinned test loudly with the number; that is the mechanism working. Everything on `spawn` is outside the ceiling. |
| D13 | A refused mint refuses; it never degrades to an unbound seat | `delegate(seat=true)` on an item whose areas are held names the holder and writes nothing, not even the delegation row. The parent picks another item, or delegates without a seat and spawns an unbound one so the divvy decides. A binding that quietly downgrades is the failure the binding exists to prevent. |
| D14 | The claim half of registration is one savepoint | Registration commits regardless. Claim, reservations and the PRD-35 link run inside one savepoint and succeed or fail as a unit; on failure the savepoint rolls back and `assigned.state` is `taken` with a reason. A claim with no link, or a link with no claim, cannot exist. |
| D15 | `taken` is what every claim path already refuses, with the reason named | A live lease held by someone else, a status outside `backlog`/`next`, a blocker, or a bounce pin held by another agent. `assigned` carries `reason` in `held`, `status:<x>`, `blocked`, `pinned`, and `held_by`. `spawn` echoes `assigned` in its own result, so the parent learns it when the child registers, not by polling. |
| D16 | The tier table is immutable per supervisor process | Changing a model is a restart. Every `spawn` reply names the adapter and model that ran; a mapped model is checked at launch as gbagent models are today, so a model that vanished fails at spawn naming itself. No reload, no TTL. |
| D17 | `until` tries each seed once per wave | On `taken` the seed stays in the loop's delegated set and the next tick takes the next free cluster. A tick with nothing delegable mints nothing and counts as an empty tick toward idle. Writes are bounded by the number of seeds. |
| D18 | Three timers, three facts | The delegation lease decides `open` versus `expired`; the link step links an unlinked row past its lease, so a late child still reads `claimed`. The item lease starts fresh at the claim. The seat TTL bounds how late a child may arrive at all. None inherits from another. |
| D19 | The delegator may not sign off its child's work | Refused on the record, not on parentage: `sign_off` and `claim_review` look up the delegation linked to the item's current builder and refuse when the reviewer is its `delegated_by`. A bounce is still allowed, because rejecting is not approving. `independent()` is unchanged; a seat-lineage child remains a separate process for every other purpose. |

## D1 — The bound seat

```
enrolments
  item_id        fk items     nullable   the item this seat will claim at registration
  delegation_id  fk delegations nullable the request that minted it
```

An unbound seat is unchanged. A bound seat is worker-role only; binding a reviewer seat to an item is refused, because a reviewer takes review through `claim_review` and must not be steered to one item by whoever minted it.

## D3 — Claim at registration

`register_agent` already consumes the seat and writes the agent. With a bound seat it continues in the same transaction: resolve the item, check it is claimable and not pinned elsewhere (the same `_is_claimable` and `pinned_elsewhere` every claim path uses), run `_try_claim` for the new agent, reserve the item's touch areas against it, and call the PRD-35 link step, which finds the delegation by lineage and links it. On any refusal the agent is still registered and the reply says `assigned: {item, state: "taken", reason, held_by}`. The claim, the reservations and the link run inside one savepoint (D14); registration itself is committed whatever the savepoint does. `_try_claim` is the one write point for claims and stays so; this is a fourth caller, not a second implementation.

## D6 — Tier resolution in the supervisor

```
gbfleet mcp   --tier cheap=gbagent:qwen3.6:35b-a3b-coding-mtp-det --tier frontier=claude:opus
gbfleet until --tier cheap=... --tier frontier=... [--request cheap|frontier]
```

`--tier` names the mapping; `--request` (replacing PRD-35's `--tier` flag on `until`, which chose only what to write on the delegation) names which tier the loop asks for. `spawn(tier=...)` resolves through the table, and the reply carries `adapter`, `model` and the child's `assigned` so the operator sees what ran and what it holds. The table is fixed for the life of the supervisor process (D16). `gbfleet doctor` reports each mapped tier's adapter and checks its model as it checks the adapter today.

---

## 6. Data model

Two nullable columns on `enrolments` (alembic `0105_bound_seats`): `item_id`, `delegation_id`. No columns on `agents`, `items` or `delegations`.

`services/fleet.py`: `issue_enrolment(..., item_id=None, delegation_id=None)`, `mint_enrolment_as(...)` the same; `register_agent` gains the claim-at-registration step and returns `assigned`. `services/delegation.py`: `delegate(..., seat: bool)` mints through `mint_enrolment_as` and records the seat on the delegation's reply. `mcp_server.py`: `delegate` gains `seat`; `register_agent` result gains `assigned`.

`gbfleet`: `Seat` gains `item: str | None`; `instruction_for` gains the bound form; `mcp.spawn` and `cli` gain `tier` and the `--tier` table; `until` mints bound seats. `gbagent`: the adapter passes `--item` from the seat.

---

## 7. Acceptance criteria

Each is a test. Sabotage the call, not the model.

1. `delegate(seat=true)` returns `enrolment_code`, and the enrolment row carries the item and the delegation id. Without `seat`, no enrolment is minted.
2. `delegate(seat=true)` by an agent whose credential cannot mint is refused with the same message `mint_enrolment` gives; the delegation row is not written either.
3. A bound reviewer seat cannot be minted; the refusal names the reason.
4. `register_agent` on a bound seat returns `assigned.state == "claimed"`, the item is `in_progress` with `claimed_by` the new agent, its areas are reserved against that agent, and the delegation reads `claimed` with `linked_by == "seat"`. All in one request.
5. `register_agent` on a bound seat whose item was claimed by someone else in between returns `assigned.state == "taken"` with `held_by`; the agent is registered; the delegation reads `superseded`.
6. `register_agent` on a bound seat whose item is bounce-pinned to another agent returns `taken` with the pin's holder.
7. `register_agent` on an unbound seat returns `assigned.state == "none"`, never a missing key.
8. Sabotage: skip the reservation step and the test that claims a colliding cluster through `claim_cluster` afterwards fails.
9. A bound seat consumed by a child that then dies leaves the item to lapse on its lease as today; nothing new is held past the lease.
10. `gbfleet spawn(tier="cheap")` with `--tier cheap=gbagent:<model>` launches the gbagent adapter with that model and the reply names both. With no mapping for the tier, refused naming the flag. With explicit `adapter`, the tier is ignored and the reply says so.
11. The bound seat's instruction file names the item and says not to call `claim_cluster`; gbagent is launched with `--item`.
12. `gbfleet until` with tier mappings mints bound seats: the `delegate` call carries `seat: true`, the `mint_enrolment` call is gone, and the spawned child registers on a seat whose item is the delegated seed.
13. `until` with a seed that reads `taken` at the child's registration records it and continues; the wave is not failed by it.
14. Manifest: `test_mcp_footprint` and `test_tool_tiers` pass with the ceiling at 14200 and the trims named.
15. Docs: `docs/mcp.md` rows for `delegate` and `register_agent`, `docs/data-model.md` for `enrolments`, AGENTS.md step 5, the generated planner prompts, and gbfleet's README state the flow. The sync guards pass.
16. `sign_off` by the agent that delegated the item's current builder is refused naming the delegation; `claim_review` skips that item for that agent; `bounce` by the same agent is accepted. Sabotage: look up the delegation by `delegated_by` instead of by the item's builder and the refusal fires on the wrong item.
17. `spawn` echoes the child's `assigned` block; on `taken` the reply names the reason and holder.
18. Operating loop: deploy; from a Claude Code session with both MCP servers, `delegate(seat=true)` one real item, `spawn(tier="cheap")` it onto gbagent, keep working, and read the Live board: the delegation reads `claimed by <child> (requested cheap, declared local, <model>)` within one poll, and later `finished` when the child moves the item. Recorded as `note` evidence with the two tool results' sizes.

---

## 8. Phasing

**PR 1, the server.** Bound seats, claim at registration, `delegate(seat=true)`, `assigned` on registration, docs and manifest pins. Criteria 1–9, 14, 15 (server half).

**PR 2, the supervisor.** `--tier` mappings, `spawn(tier)`, the bound instruction, gbagent `--item`, `until` on bound seats, gbfleet docs and prompts. Criteria 10–13, 15 (fleet half), 16.

---

## 9. Risks and open questions

### Risks

- **A bound seat is a steered claim.** The divvy's collision check is bypassed for that item; only the item's own areas are reserved. A parent that binds two colliding items to two seats has created the collision the divvy prevents. Mitigation: `delegate(seat=true)` refuses when the item's areas are held or reserved by someone else, the same check `claim_cluster` makes, and says who holds them.
- **Claim at registration is a write inside `register_agent`.** Registration is the one call that must never be refused for a spawned child. D4 keeps it so: the claim can fail, the registration cannot. The test for criterion 5 is the one that proves it.
- **Tier mapping is per machine.** Two supervisors with different `--tier` tables give "cheap" two meanings. The reply names adapter and model on every spawn, and the child declares its model at registration, so the record shows which cheap ran.

### Open questions — closed 2026-09-04 (grill)

1. The delegator may not sign off its child's work (D19). Choosing the item, writing the brief and picking the tier is co-authorship of the plan; review is the second opinion. Refused on the delegation record, never on parentage.
2. A seat consumed into `taken` is spent, like any consumed seat. The child registered; the item was not there. Reissue stays what it is for: a dead child.
3. A bound seat may be minted by whoever may mint today: planner or all-in-one. A lone operator session handing one item to a local model is the first use this PRD is for.
4. The grill's questions on refusal fallback, atomicity, the meaning of `taken`, tier-table mutability, the loop on a taken seed, the three timers and the manifest trims are D13–D18 and the revised D3, D6 and D12.

---

## 10. Prior art

- PRD-35: the delegation record, lineage by seat, requested versus declared tier, and the divvy-prediction risk this PRD closes.
- PRD-22 D-b: a spawned child is a separate process with no parent; this PRD binds it to an item without touching that.
- PRD-19: seats, consumption, reissue, and the planner-only mint gate this PRD reuses.
- PRD-24 S7: gbagent's `--item`, the half of this flow that already exists on the child.
- `harness-kit/claude-code/delegation` (reviewed 2026-09-04): the shape classifier's one durable idea, delegate whole units, kept here as D10; its hook-based enforcement not carried.
