# PRD-35 — Delegation as a ledger fact: brief, record, tier

**Ledger id:** GRPH-P35
**Status:** approved — grill complete 2026-09-04; answers absorbed into D14–D21, the revised D7/D9/D10/D12, and §9. v1.0. The PRD-17 D6 allocation shape, applied to the agent that does not exist yet.
**Depends on:** PRD-17 (roles, `parent_agent_id`, `independent()`, `propose_allocation` / `assign_role`) · PRD-19 (seats) · PRD-34 (the feed the `delegate` call lands in) · PRD-22 / PRD-30 (the producers that spawn)
**Complemented by:** `scripts/gen_subagents.py` (the prompts this PRD shrinks) · Fleet view (provisioning) · Live board (where delegations show)
**Touches:** `backend/app/models/__init__.py` (`Delegation` new) · `backend/alembic/versions/0104_*` · `backend/app/services/delegation.py` (new) · `backend/app/services/items.py` and `cluster.py` (link step on claim) · `backend/app/services/live.py` · `backend/app/services/agent_calls.py` (`TARGETS`) · `backend/app/mcp_server.py` (`delegate`, `get_item_details.brief`) · `backend/app/services/tool_tiers.py` · `web/src/features/live/LiveView.tsx` · `web/src/lib/types.ts` · `scripts/gen_subagents.py` and its outputs · `fleet/src/gbfleet/supervisor.py` · `AGENTS.md` · `docs/mcp.md` · `docs/data-model.md`

---

## 1. Overview

<!-- framing -->

A Graphban fleet delegates in four places today: a Claude Code planner spawning a `gb-implementer`, a Cursor planner spawning its native subagent, a Codex planner reading `.codex/agents/*.toml`, and `gbfleet` spawning a vendor CLI on a seat. All four follow the same rules, because one generator writes the same prompt body into each tool's format. None of them record that a delegation happened, what it asked for, or what showed up to do it.

This PRD makes delegation a **ledger fact** with three parts, in the shape the fleet already uses for allocation (PRD-17 D6: the server proposes, the planner commits):

1. **The brief.** What a delegate must be told, produced by the server from the item, so every harness hands the same packet and the generated prompts stop restating it.
2. **The record.** One row per delegation: who delegated which item, into which lane, at which tier, and what claimed it. With a named third state for a delegation nothing ever claimed.
3. **The tier, requested.** Cheap or frontier is a field the delegator writes on the record. The server **suggests** a tier in the brief, from evidence it observed, and never applies one.

The load-bearing invariant:

**The server states the rule and records the outcome. The harness spawns. The delegate declares what it is. No part of this PRD lets the server choose a worker, spawn a process, or rate an item's difficulty.**

### 1.1 What this is not

- **Not a scheduler.** The server does not spawn and does not decide who builds. `propose_allocation` already proposes and `assign_role` already commits, for agents that exist. This PRD covers the agent that does not exist yet: the child a planner is about to spawn.
- **Not a difficulty model.** Touchpoint breadth and task class do not predict whether a cheap model can build an item. The only signal the server has is what happened last time, and that is the only thing the suggestion reads.
- **Not outcome statistics.** The record makes "which model builds which lane" answerable later. No view, no rule, and no ranking in this PRD; the sample is small and biased by construction (the items that reach frontier are the ones cheap already failed).
- **Not verification of the declared model.** `capabilities.model` is a coordination fact like `capabilities.instance` (PRD-17): a delegate that misreports it corrupts only its own record.

---

## 2. Problem

<!-- framing -->

Verified against the tree at `0c052b03`, 2026-09-04.

### 2.1 Delegation rules are prompt text, four times over

`scripts/gen_subagents.py` carries the rules as prose in the planner body: route `web/**` to `gb-frontend` and everything else to `gb-implementer`, scout first on open questions, never two items from one cluster in parallel, and "put everything the worker needs in the delegation prompt: the item id, the spec summary, the predicted touchpoints, and the relevant invariant". The generator emits that into `.claude/agents/`, `.cursor/`, and `.codex/agents/`. The model tier is a constant per agent kind (`tier: "cheap"` on the implementer, `"frontier"` on the planner) and three tables map it to each tool's knob: `CLAUDE_MODEL`, `CURSOR_MODEL`, `CODEX_EFFORT`.

The text is consistent because it is generated. Whether a given harness followed it is unknowable, because nothing is written when a parent delegates.

### 2.2 Nothing records a delegation

The only trace is `Agent.parent_agent_id`, set at `register_agent` when the child declares it, and read by `independent()` to stop a child reviewing its parent. It exists only once the child has registered. A child that dies before registering, or registers and never claims, leaves nothing. On the Live board the parent looks idle and the item looks ready. That is the absence-reads-as-clean class, again.

### 2.3 The model is declared and joins nothing

`register_agent` accepts `capabilities: {vendor, model, tier, readonly, host}`. `vendor` is read by `claim_review` to prefer cross-vendor pairing. `model` and `tier` are stored and read by nothing. The label carries `"<model> @ <host>:<worktree>"` as free text. PRD-24's finding that one local model fails every build and another gets signed off is a memory note, not a row.

### 2.4 Escalation is a sentence in the orchestrator prompt

"If the same item bounces twice, the problem is usually the item, not the worker." The server stores one `bounce_reason` per item, overwritten on the next bounce. It does not store how many attempts an item has had, at what tier, by which model, or how each ended. A planner re-delegating after a bounce has to reconstruct that from `events`.

### 2.5 The manifest is full

The full manifest measures 14199 tokens against a 14200 ceiling (`test_mcp_footprint.MEASURED_TOKENS`). Every property this PRD adds to a tool schema is paid for by a trim somewhere else. Raising the ceiling is a decision the footprint test argues against by name, and this PRD does not make it.

---

## 3. Goals

1. A delegation is a row with a named state, visible on the Live board under the delegator, including the state where nothing claimed it.
2. One brief per item, produced by the server, that a Claude Code, Cursor, Codex, or `gbfleet` parent hands to its child unchanged.
3. Tier and lane are written by the delegator on the record. The brief suggests both and names the evidence for each suggestion.
4. What claimed the delegation, and the model and tier it declared, are on the record next to what was requested. A mismatch is shown, never refused.
5. Re-delegation after a bounce carries the bounce reason and the previous attempt into the next brief.
6. The generated subagent prompts shrink: the rules they restate become "call `delegate` and paste the brief".
7. No manifest ceiling raise. No new read tool.

## 4. Non-Goals

- Spawning, adopting, or stopping processes. That is `gbfleet` (PRD-22) and the unattended loop (PRD-30).
- Delegating to an agent that already exists. `assign_role` and its directive downlink cover that; a live agent takes work by claiming, not by being told.
- Computing difficulty from the item. See 1.1.
- Automatic tiering, automatic re-delegation, or any write the server makes to a delegation on its own beyond deriving state from time and from the item.
- Refusing a claim because the claimant's lane or tier does not match the request.
- Tier-aware review pairing (a cheap build gets a frontier reviewer). Plausible, deferred until the record shows whether it is needed.
- Outcome views or statistics per model. Deferred until there is something to read.
- Editor-side hooks reporting what the child did. PRD-34 built the feed those would land in; this PRD adds nothing to it beyond the `delegate` call itself.

---

## 5. Key decisions

| # | Decision | Why |
| --- | --- | --- |
| D1 | Three parties, one shape: server states, harness executes, delegate declares | Matches `propose_allocation` / `assign_role`. The server has no handle on a process it did not start and must not pretend to. |
| D2 | The brief is a block on `get_item_details`, not a new tool | The manifest is at 14199/14200. A read the planner already makes gains a field; nothing new to discover or pay for. |
| D3 | One new write, `delegate`, in the `fleet` tool tier | Intent has to be written before the child exists, or the never-claimed state cannot exist. `fleet` tier because a lone agent never delegates. |
| D4 | Tier is requested per delegation, never computed, never stored on the item | The item does not have a difficulty; an attempt has a tier. Per-attempt tiers are what make the escalation history readable. |
| D5 | The brief's suggestion carries a `basis` and is text only | `basis` is one of `none`, `bounced`, `blocked`, `released`, `previous`. `none` suggests cheap. The delegator commits by passing `tier` to `delegate`; the suggestion is never copied in as a default. |
| D6 | Lane is suggested from touchpoints, committed by the caller | `web/**` only is `frontend`; anything else is `backend`; both is `mixed`. Same `basis` discipline: the brief names the touchpoints that decided it. |
| D7 | A delegation links only to a claim by a declared child of the delegator | `claimant.parent_agent_id == delegator` is the one link rule. A claim by anyone else closes the delegation as `superseded` with the claimant's id. The parent then reads the truth: my child never came, someone else took the item. |
| D8 | Requested and declared sit side by side and never gate | The claimant's `capabilities.model` and `capabilities.tier` are copied onto the record at link time. `requested cheap, declared frontier` is a row, not an error. Absent declaration is `undeclared`, never treated as a match. |
| D9 | Five states, derived, never written: `open`, `claimed`, `finished`, `expired`, `closed` | `open` until linked; `expired` when open past the lease; `finished` when the linked item leaves `in_progress` or `review`, with `outcome` read from the item: `signed_off`, `bounced`, `blocked`, `released`; `closed` with `reason` `withdrawn` (the delegator re-delegated) or `superseded` (someone else claimed). |
| D10 | Re-delegation reads the whole history into the brief | `previous` is the immediate prior attempt with `bounce_reason`, `requested_tier`, `declared_model`, `outcome`; `attempts` is every prior delegation on the item, oldest first, one line each. Basis becomes `bounced` or `blocked` or `released`. Escalation is a new `delegate` call by the planner, never a server write. |
| D11 | Live board shows delegations under the delegator | "2 delegated: 1 claimed by GRPH-A140 (cheap, haiku), 1 open 4m". The `delegate` call itself is an observed feed row via PRD-34 with the item as target. |
| D12 | Generated prompts drop the rules the brief now carries | Planner body becomes: `get_item_details`, read `brief`, `delegate`, paste the returned brief into the spawn. The tier tables in `gen_subagents.py` stay and are applied per harness: where the model is chosen at call time (Claude Code) the planner prompt carries the tier table and passes the model on the spawn; where it is fixed per file (Cursor, Codex) the generator emits a frontier variant of each writing agent and the planner picks the file by lane and tier. `gbfleet` and the PRD-30 loop call `delegate` before spawning a seat. |
| D13 | Manifest cost is paid by trims in the same PR | `MEASURED_TOKENS` and `CORE_TOKENS` move by the measured amount with the trims named in the commit. The ceiling stays 14200. |
| D14 | The owner withdraws by re-delegating | A second `delegate` by the same delegator on its own open delegation closes the first as `withdrawn` and opens the new one in one call. The delegator is the only actor who knows its child is dead. Anyone else is refused with the open delegation's id and age until it expires. No polling contract, no grace window. |
| D15 | A bounce pin refuses delegation, except to the pinned author's parent | `bounce` pins the item to its author for one lease. `delegate` during the pin is refused with `pinned_until`, unless the pinned author's `parent_agent_id` is the caller: that is the planner re-tasking its own child's bounce. |
| D16 | `brief.text` is prose for a prompt, carries no suggestion, and is never parsed | It is derived from the fields, human-readable, and excludes `lane` and `tier` suggestions so a pasted brief cannot become the default D5 bans. The child that wants structure calls `get_item_details` itself. |
| D17 | Brief caps | Summary is the first paragraph up to 600 characters; lessons five; touchpoints all; `attempts` one line each, uncapped. Response size does not touch the manifest ceiling, which counts schemas only. |
| D18 | `lease_seconds` is copied at write, deliberately | `expired` is computed from the row's own lease, so changing the setting later does not move an existing delegation's expiry. Auditability over policy reach. |
| D19 | The board fetch is bounded | Everything open, plus the last ten `finished`, `expired` or `closed` within `agent_call_retention_days`, in one grouped query for the whole board. Older history is on the item via `brief.attempts`. |
| D20 | The brief inherits the item's scope, nothing more | Lessons are the item's linked shards, already readable by any key on the project. A child on the same credential sees what its parent sees; a child on a narrower key gets the item's read, not the parent's. No new access path. |
| D21 | PR 1 ships the payload field, PR 2 renders it | The Live payload gains `delegations` in PR 1; the web ignores unknown fields, so deploying PR 1 alone changes nothing on screen and breaks nothing. |

## D1 — Server states, harness executes, delegate declares

The server can do three things to a delegation: write what the delegator asked for, link what turned up, and derive state from time and from the item. It cannot see the spawn, so it records nothing about it, and it never refuses a claim on lane or tier. A harness that cannot honour the requested tier (this endpoint has refused standard model ids for fresh subagents in earlier sessions) spawns what it can, and the child declares what it is. The record then reads `requested cheap, declared frontier`, which is the truth, rather than reading as satisfied.

## D2 — The brief

`get_item_details` gains `brief`:

```
brief: {
  item: "GRPH-701", title, summary,            # summary = first paragraph of description
  touchpoints: [...],                          # predicted + landed, deduped
  blocked_by: [...], ready: bool,
  checklist: "mcp_tool" | "migration" | "frontend" | "docs" | null,   # AGENTS.md task class
  lessons: [{id, text}],                       # linked shards, capped 5
  lane: {value, basis: [touchpoints that decided it]},
  tier: {value, basis: "none"|"bounced"|"blocked"|"released"|"previous"},
  previous: {requested_tier, declared_model, outcome, bounce_reason} | null,
  attempts: [{requested_tier, declared_model, outcome}],   # every prior delegation, oldest first
  pinned: {to, until} | null,                  # bounce pin, when held
  text: "..."                                  # the facts above, minus lane and tier, as prose
}
```

`text` is what a parent pastes into a spawn prompt. It is derived from the fields above and nothing else, so it cannot say something the fields do not. It excludes the `lane` and `tier` suggestions (D16), so the pasted block cannot smuggle a default into the `delegate` call. It is prose for a model, not a format for a parser: a child that wants structure calls `get_item_details` itself. It does not include AGENTS.md; the child reads that itself. Caps are D17.

## D3 — `delegate`

```
delegate(id, lane, tier, note="") ->
  {delegation_id, state: "open", brief: {...}}
```

Refused when the item is not `ready`, when the caller holds the item's lease, when the item is bounce-pinned to an agent that is not the caller's child (D15, with `pinned_until` in the error), or when another delegator's open delegation exists for the item (returned with its id and age). The caller's own open delegation is not a refusal: it is closed as `withdrawn` and replaced in the same call (D14). Allowed for any role with the `fleet` tier; the orchestrator prompt's "do not claim work" stays true because `delegate` claims nothing.

`lane` and `tier` are required. There is no default, because a default is the server choosing.

## D7 — Linking

`claim_item`, `claim_next`, `claim_cluster` and `next_cluster` all pass through one step after a successful claim: find the open delegation for the item. If the claimant's `parent_agent_id` is the delegator, link it: write `agent_id`, `declared_model`, `declared_tier`, `claimed_at`. If the claimant is anyone else, including the delegator itself, close it as `superseded` with the claimant's id in `closed_by`. There is no claim-order link: a stranger's claim is not evidence that the child arrived, and a record that said `claimed` would hide the parent's silence. The parent doing the work it delegated is exactly the failure the record exists to show, and it reads `superseded by <parent>`.

## D9 — States

State is a function of the row, the clock and the item, computed at read time, never stored:

| State | When | Shown as |
| --- | --- | --- |
| `open` | no `agent_id`, `created_at + lease > now` | "open 4m" |
| `expired` | no `agent_id`, past the lease | "expired, nothing claimed" |
| `claimed` | linked, item still `in_progress` or `review` | "claimed by GRPH-A140 (cheap, haiku) 12m" |
| `finished` | linked, item elsewhere | "signed off" / "bounced: <reason>" / "blocked" / "released" |
| `closed` | `closed_reason` set | "withdrawn" / "superseded by GRPH-A150" |

`expired` is the third state. An operator who sees it knows a spawn failed silently. Nothing today can show that. `closed` is stored, because it records an event (a withdrawal or a stranger's claim) that the clock and the item cannot reproduce; the other four are derived.

---

## 6. Data model

`delegations` (new, alembic `0104_delegations`):

| column | type | notes |
| --- | --- | --- |
| id | str pk | |
| project_id | fk projects | |
| item_id | fk items | index |
| delegated_by | fk agents | the planner |
| agent_id | fk agents nullable | the child, set at link |
| closed_reason | str(16) nullable | `withdrawn` or `superseded` |
| closed_by | fk agents nullable | who claimed, on `superseded` |
| closed_at | datetime tz nullable | |
| lane | str(16) | as requested |
| requested_tier | str(16) | `cheap` or `frontier` |
| declared_model | str(64) nullable | from claimant capabilities at link |
| declared_tier | str(16) nullable | same |
| note | str(200) | delegator's own words |
| created_at | datetime tz | |
| claimed_at | datetime tz nullable | |
| lease_seconds | int | copied from settings at write, so `expired` is stable |

Indexes: `(item_id, created_at)`, `(delegated_by, created_at)`. No columns on `Item`. No columns on `Agent`.

`services/delegation.py` (new): `brief(db, item)`, `delegate(db, agent, item, lane, tier, note)`, `on_claim(db, item, claimant)` (link or supersede), `state(row, item, now)`, `for_board(db, project_id, now)` (D19, one query). `services/live.py` reads `for_agent`. Routers stay free of model imports, as `routers/live.py` is today.

`agent_calls.TARGETS` gains `delegate` with the item id as target.

---

## 7. Acceptance criteria

Each is a test. Sabotage the call, not the model.

1. `get_item_details` on an item with no history returns `brief.tier.basis == "none"` and `brief.tier.value == "cheap"`. Removing the basis field fails the test.
2. An item whose only touchpoints are under `web/` briefs `lane.value == "frontend"` with those paths in `basis`. Add one backend path and it becomes `mixed`.
3. `brief.text` contains every touchpoint, the item id, the checklist name and each lesson id. Delete one from the fields and the text loses it too.
4. `delegate` without `tier` is a validation error. Without `lane`, the same.
5. `delegate` on a non-ready item is refused with the blocker named. On an item with an open delegation, refused with that delegation's id.
6. `delegate` by the agent holding the item's lease is refused.
7. After `delegate`, `claim_item` by an agent whose `parent_agent_id` is the delegator links. By an unrelated agent, the delegation reads `closed`, `superseded`, with that agent in `closed_by`. By the delegator itself, the same, with the delegator in `closed_by`.
8. Each of `claim_next`, `claim_cluster`, `next_cluster` links and supersedes the same way. One test per entry point; the sabotage removes the `on_claim` call from that one path.
9. The claimant's `capabilities.model` and `capabilities.tier` are copied at link. A claimant with no capabilities reads `declared_tier == "undeclared"`, and the summary line says so.
10. `requested cheap` with `declared frontier` links, reads `mismatch: true`, and refuses nothing.
11. `state` is `open` before the lease and `expired` after, on the same row with the clock moved.
12. A linked delegation whose item moves to `done` via `sign_off` reads `finished` with `outcome == "signed_off"`. Via `bounce`, `outcome == "bounced"` with the reason. Via `release_item`, `released`. Via `update_item(status="blocked")`, `blocked`.
13. After the pin lapses, a second `delegate` on the bounced item briefs `tier.basis == "bounced"`, `tier.value == "frontier"`, and `previous` holds the first attempt's requested tier, declared model, outcome and the bounce reason. After three attempts, `attempts` lists all three in order; `previous` is the third.
14. The suggestion is not a default: calling `delegate(tier="cheap")` on the bounced item writes `cheap`.
15. Live board: the delegator's row carries `delegations` with counts per state and the oldest open age, from one query for the board (sabotage: a second delegator's rows leak into the first). The row for an agent with no delegations carries `delegations: null`, not `[]`. The web renders "no delegations" for null. With the field absent from the payload, as after a PR 1 deploy without PR 2, the web renders the row as it does today.
16. Live board: an expired delegation renders "expired, nothing claimed" with the item id. Sabotage: change the state to `open` in the payload and the string disappears.
17. The `delegate` call appears in the delegator's PRD-34 feed with the item as target.
18. Router source for the live and fleet routers does not contain `Delegation`.
19. `test_mcp_footprint` and `test_tool_tiers` pass with the new tool and property, with the ceiling unchanged at 14200 and the trims named in the commit.
20. `delegate` is absent from a manifest whose key lacks the `fleet` tier, and still dispatches for one that has it.
21. Generated agent files: `gb-planner` and `gb-orchestrator` bodies mention `delegate` and `brief` and no longer contain the sentence beginning "Because subagents start with a clean context window". `test_subagent_fleet` passes against the regenerated files.
22. `gbfleet up` calls `delegate` once per seat it is about to spawn for a specific item, and the spawn prompt contains `brief.text`. A seat spawned without an item makes no `delegate` call.
23. Operating loop: deploy, delegate one item from a Claude Code session to a spawned child, read the Live board, and kill a second child before it registers. The first reads `claimed`; the second reads `open` and then `expired`. Recorded as `note` evidence, with the measured length of `brief.text` for the larger item.
24. `delegate` during a bounce pin held by a stranger is refused with `pinned_until`. Held by the caller's declared child, it is accepted.
25. A second `delegate` by the same delegator on its open delegation returns a new id; the first reads `closed`, `withdrawn`. By a different delegator, refused with the first's id and its age in seconds.
26. `brief.text` does not contain the strings `lane:` or `tier:` or the suggestion values; the structured fields do. Sabotage: append the suggestion to the text and the test fails.
27. `brief.text` summary is cut at the first paragraph and 600 characters; a sixth linked lesson is absent from `lessons` and from the text.
28. Changing `lease_seconds` in settings after a delegation is written does not move its `expired` boundary.
29. Generated Cursor and Codex outputs include `gb-implementer-frontier` and `gb-frontend-frontier` with the frontier knob; the Claude Code planner file contains the tier table and no frontier variant files exist for it.
30. Deploying PR 1 alone: the Live payload carries `delegations`, and the pre-PR-2 web renders unchanged. A test pins that the web's live types tolerate the unknown field.

---

## 8. Phasing

**PR 1, the fact.** `delegations` table and migration, `services/delegation.py`, `brief` on `get_item_details`, `delegate` tool, link step on the four claim paths, states and outcomes, feed target. Manifest trims. Live payload field. Criteria 1–14, 17–20, 24–28, 30 (payload half).

**PR 2, the surfaces and the producers.** Live board `delegations` block, web rendering with the null and expired states, `gen_subagents.py` prompt changes and regenerated files, `gbfleet` calling `delegate` before spawn, AGENTS.md and docs. Criteria 15, 16, 21, 22, 23, 29.

Nothing in PR 2 changes what PR 1 wrote. A reader of the ledger after PR 1 already sees delegations, in `get_item_details` and in the `delegate` result.

---

## 9. Risks and open questions

### Risks

- **A child that forgets `parent_agent_id` reads as a stranger.** Its claim supersedes the delegation instead of linking it. The generated prompts and `gbfleet` both pass the parent id, so the ordinary paths are covered; a hand-spawned child that omits it produces a `superseded by <its own child>` row, which is at least visible and points at the missing argument.
- **The suggestion becomes a default in practice.** Every planner passes the suggested tier back unchanged, and `basis` is decoration. The mitigation is D5: no default, so the planner types the word. Whether it thinks first is the planner's job.
- **Manifest trims are found on the day.** 14199 of 14200 leaves no room; the trims will come from description text. PRD-34 did this twice and the discipline is known, but the PR that ships `delegate` is the one that pays.
- **`brief.text` drifts from the fields.** Criterion 3 pins it. The text is generated from the fields in one function and nowhere else.

### Open questions — closed 2026-09-04 (grill)

1. `expired` releases nothing: the item was never claimed. The owner retries with `delegate`, which withdraws and replaces (D14); anyone else waits for expiry.
2. `lane` stays at three values. That is what the touchpoint rule can decide; anything else is the delegator's `note`.
3. Re-tasking a live seat stays a non-goal. `assign_role` covers it. Revisit when PRD-30 builds.
4. The grill's questions on linking, brief determinism, bounce history, mismatch cost, static prompts, sequential delegations, dead children and board load are answered in D14–D21 and the revised D7, D9, D10 and D12.

---

## 10. Prior art

- PRD-17 D6: `propose_allocation` proposes, `assign_role` commits. This PRD copies the shape for the agent that does not exist yet.
- PRD-17 `independent()` and `parent_agent_id`: the one existing trace of a delegation, read only at review time.
- PRD-22 supervisor and PRD-30 unattended loop: the producers that will call `delegate` before spawn.
- PRD-24 gbagent: the finding that model choice decides whether the arc works at all, which this PRD turns into a row.
- PRD-34 feed: where the `delegate` call shows up as an observed row.
- `scripts/gen_subagents.py`: the tier tables and the prompt bodies that this PRD shrinks.
