# PRD-17 — Fleet roles: server-arbitrated agentic role assignment

**Ledger id:** GRPH-P17
**Status:** approved — reached by completing the grill (PRD-15) on 2026-08-09; all four
dimensions graded `resolved`. This line said `draft` until 2026-08-20, while the ledger
had recorded the approval for eleven days and every slice shipped against it.
**Depends on:** AL-192 (collision clustering) · AL-201 (worktree spike) · AL-213 (sub-agent roster) · AL-78 (scope-gated manifest)

## 1. Overview

A human opens several terminals. The one they sit in is the **planner/orchestrator**. The
others — any mix of Claude Code, Codex, Cursor, Grok Build, opencode — connect to the same
Graphban MCP endpoint, register, and are **assigned a role and a non-colliding slice of
work** based on how many agents showed up and what the backlog looks like.

Graphban already has the queue: `claim_next` takes an atomic lease
(`services/items.py:400`), `prioritization.ready()` gates on dependencies
(`services/prioritization.py:66`), and `collision_clusters` partitions items into sets that
provably do not touch the same files (`services/collision.py`). What it does not have is any
idea that **agents exist**. `agent_id` is a self-declared string that defaults to the API
key's name (`mcp_server.py:1571`), so three terminals sharing a key are one agent to the
server, and nothing counts, roles, or arbitrates between them.

This PRD adds the missing layer: **agents as first-class, roles as server-enforced
invariants, the collision divvy exposed to the fleet that needs it, and a Fleet view that
makes the human the transport MCP cannot be.**

### Two postures, and which one these invariants describe

**This PRD is an OPTION, not a replacement.** Graphban has two deployments and both are
first-class:

- **Single-agent (the default).** One developer, one agent, one key — no orchestrator, no
  fleet. **The human is the reviewer.** None of the server-side gates below apply: an
  unregistered agent, or one registering on an unnarrowed credential, is `all-in-one` and
  unrestricted, and marks its own work done. That is correct, not a hole. The review happens
  where it always did — a person reading the diff.
- **Fleet (the power-user posture).** Several agents, possibly across vendors and machines,
  where no human is watching every hand-off. Roles are specialised, the server arbitrates,
  and everything below applies.

Every invariant in this document is a property of the **fleet** posture. Stated unqualified
they read as universal, and a reader would build on a guarantee half the deployments do not
have — which is the failure this PRD exists to prevent, arriving in prose.

The two are not separate products and there is no migration between them: the same substrate,
held two ways. An all-in-one agent becomes a fleet member the moment somebody assigns it a
role, and `fleet_status` names which posture a project is in rather than leaving a reader to
infer it from a count.

### The load-bearing invariant (fleet posture)

**An agent cannot pass its own work.** Every other rule here follows from it. Today `review`
is a status an item sits in — nothing routes it to a *different* agent, and nothing stops the
author from marking their own work `done`. That is the one failure a fleet is uniquely
positioned to fix: with more than one agent in the room, self-review stops being a
procedural discipline and becomes a `WHERE claimed_by != :caller` clause.

**The ban is keyed on agent identity, never on role — so a role change cannot launder it.**
The obvious attack on a dynamic-role system is to promote a worker to reviewer while it holds
its own item. It does not work: an agent's id does not change when its role does, so the
promoted agent is still that item's `claimed_by` and `claim_review` still filters it out. Two
independent gates enforce this — the `claim_review` filter, and an assertion at `sign_off`
that `Item.reviewed_by != Item.claimed_by` — because a single gate keyed on a *query* is one
refactor away from being keyed on the caller's current role instead of on authorship.

**And the ban should reach past identity to vendor.** A Claude reviewer approving Claude work
is a *different agent* but not a different error distribution — same training, same blind
spots, same things it does not think to check. `Agent.capabilities.vendor` is already recorded
at registration, so `claim_review` can prefer a reviewer whose vendor differs from the
author's, falling back to agent-distinct when the fleet lacks the diversity. This upgrades the
invariant from preventing **self**-review to preventing **monoculture** review, and it is the
concrete payoff for running four heterogeneous windows rather than four identical ones.

### What this is not

The existing roster in `.cursor/agents/` (AL-213) is *prompt-primed, single-host,
single-vendor, and unenforced* — Cursor sub-agents inside one Cursor session, told how to
behave. This is *server-arbitrated, cross-host, cross-vendor, and enforced*: independent
processes on independent machines that the server keeps honest. They compose —
`scripts/gen_subagents.py` is the client half and gets extended, not replaced (§7).

The same holds for the in-session **orchestrator** patterns those clients now ship. This PRD
does not replace them; one becomes a single worker in the fleet and keeps orchestrating
whatever it claimed. §9 works the relationship through in full, including the two places it
constrains what we can build.

## 2. Goals

- **G1** — Graphban knows how many agents are live, what each can do, and what each holds.
- **G2** — Roles (`planner` / `worker` / `reviewer`) are enforced by the server, so a
  drifting or adversarial client cannot exceed its role. Refusal is an `unauthorized` tool
  error, not a paragraph.
- **G3** — An item cannot reach `done` without a **second** agent's verdict.
- **G4** — Concurrently assigned work is provably non-colliding, so N workers in N worktrees
  do not trash each other.
- **G5** — Role allocation responds to the fleet: 4 agents and 12 ready items should divvy
  differently than 2 agents and 3 items, and the human decides.
- **G6** — Vendor-neutral. Anything that speaks MCP can be a fleet member.
- **G7** — Standing up a fleet member is one copy-paste. The Fleet view mints the
  role-scoped key, the client config, and the role prompt together — and shows, at a glance,
  who is doing what.

## 3. Key decisions

### D-a. Roles are dynamic; keys are the ceiling

The obvious tension: role enforcement wants roles pinned to a credential, and fleet
allocation wants roles decided at connect time. Resolve it by making them **two different
things**.

- **`ApiKey.roles`** — the set of roles this credential is *eligible* for. The security
  boundary. A worker key can never become a planner, whatever it claims at registration.
- **`Agent.active_role`** — the role this registered agent is *currently* assigned, chosen
  from `eligible ∩ allocation` when it registers. The scheduling decision.

A stolen worker key is still only ever a worker. A live worker can still be re-tasked to
reviewer between waves *if its key allows it*. Both answers are satisfied and neither is
weakened.

### D-b. Enforce at call time, not at manifest time

AL-78 gates `tools/list` by scope, and the instinct is to extend that to roles. It does not
work here: **`tools/list` is fetched once at client connect, before `register_agent` has
run**, and Graphban's MCP endpoint returns single-JSON with no SSE
([mcp.md](mcp.md)) — so the server has no channel to send
`notifications/tools/list_changed` when a role is later assigned or swapped.

So: the manifest advertises the **union of the key's eligible roles**, and the **call gate**
is the enforcement point.

**This is not an HTTP status.** MCP tool failures are JSON-RPC *tool errors* carrying a
stable machine-readable `code` from `app/errors.py`; the transport response is a normal 200
with an error payload. A role refusal raises `authz.Forbidden`, which the dispatcher already
maps to `_tool_error(id_, "unauthorized", ...)` — no new error class, no new code:

```json
{"code": "unauthorized",
 "message": "sign_off requires role 'reviewer'; AGT-3 is registered as 'worker'",
 "hint": "your work moves to review; a reviewer takes it from there"}
```

`hint` is the existing machine-readable next-step channel (AL-47), so an agent can branch on
the refusal without parsing prose.

This is strictly stronger than a lean manifest anyway — a manifest can only *fail to
mention* a tool, while the gate *refuses* it. Trimming the manifest per active role is a
later nicety, and it needs SSE on `/api/mcp` first (non-goal, below).

**Persistent non-compliance is quarantined.** An agent that ignores a directive and keeps
calling its old role's tools gets the refusal each time, and after ~3 refused calls
(configurable) the server force-releases its items and reservations and marks it `offline`,
recording the quarantine to the ledger. A drifting agent holding a cluster is strictly worse
than no agent — it blocks the divvy while producing nothing. Refusal and network silence are
indistinguishable to the server and treated identically; the quarantine rule applies only to
an agent that is demonstrably alive and calling tools.

### D-c. One worktree per worker, and the reviewer needs the branch

Parallel workers in one working tree is not a fleet, it is a merge accident. Registration
therefore carries `worktree` and `branch`, and **the branch travels with the item**: a
reviewer in a different terminal on a different machine has nothing to review unless the
handoff names the branch and the files touched. `claim_review` returns both.

### D-d. Reservations, not just partitions

`collision_clusters` partitions a *snapshot*. As work lands, actual touchpoints replace
predicted ones and the partition moves under the fleet's feet. Handing out clusters from a
stale snapshot re-introduces exactly the collisions the divvy prevents. So an assignment
**reserves its touch-areas** for the lease's duration, and every subsequent hand-out is
checked against *in-flight reservations*, not only against the static partition.

Reservations are written **in the same transaction as the claims they justify**, never as a
follow-up write, so there is no window in which items are claimed but their areas
unreserved. Two workers racing for overlapping clusters resolve through the existing
optimistic guard (`items.py:_try_claim`): the loser rolls back and retries with the next
candidate.

**Lifecycle: no background job.** `expires_at` rides the lease horizon and is extended by
`heartbeat`; rows are deleted on `sign_off` / `release_item` / `bounce`, and otherwise
expire *lazily at read time*, the same way `_is_claimable` already handles stale leases. A
sweeper would add a failure mode lazy evaluation cannot have — a stopped sweeper silently
freezing the divvy.

**When a prediction is wrong and two live clusters do collide, let it ride.** No abort and
no re-partition of in-flight work: record the collision with both agent ids and the
overlapping area, let the actual touchpoints replace the predictions, and compute the *next*
partition from ground truth. Both agents are mid-build, and aborting throws away real work to
prevent a merge conflict git will surface anyway. The Fleet view flags the overlap so the
human merges deliberately.

### D-e. The human is the uplink; the poll response is the downlink

MCP is client→server. The server cannot wake an idle terminal, and no mainstream client
starts working because a server sent a notification. That gap is real, and it is closed by
two channels rather than by pretending MCP has a third:

**Uplink — the human, via the Fleet view.** A process cannot be spawned by the server, but a
human can open a terminal and paste into it. So the Fleet view (D5) makes that paste
*one action*: pick a role, get a scoped key, the client config for whichever tool that
terminal runs, and the role prompt — together, already filled in. The human is not a
workaround for the missing push; they are the correct actor for the two decisions that
should never be automatic anyway — **issuing a credential** and **admitting an agent to the
fleet**.

**Downlink — the directive envelope.** Once an agent *is* running, the server does have a
channel: the response to whatever the agent polls next. Every fleet tool response carries an
optional `directive`, and the orchestrator's intent rides along:

```json
{ "claimed": false,
  "directive": { "type": "role_change", "role": "reviewer",
                 "reason": "AGT-2 finished; review queue is 4 deep",
                 "next": "call claim_review — your worker tools now return unauthorized" } }
```

A role change is not an error, so it does not arrive as one. The agent's loop prompt says:
*if a response carries a directive, adopt it and continue.* Reassignment therefore takes
effect on the agent's next poll with no re-priming, no reconnect, and no new transport —
`Agent.role_assigned_at > role_acked_at` is the whole mechanism, and the poll that carries
the directive acks it.

The refusal path stays as a backstop: an agent that ignores its directive and calls a
worker tool anyway gets the §D-b `unauthorized`, which names the role it has now.

**There is at most one outstanding directive per agent, ever — it is not a queue.** If the
planner issues worker→reviewer and then reviewer→worker before the agent polls, the agent
receives *one* directive saying it is a worker. That is correct rather than lossy: a role
change superseded before delivery was never true of the agent, and replaying it would make
the agent briefly adopt a role the fleet had already abandoned. Ordering is not a concern
because there is nothing to order.

**Reassigning an agent that holds work releases that work.** `assign_role` returns the
agent's items to `next` (original `sort_order` kept) and drops its reservations, in the same
transaction as the role write — the lease does not transfer and the item is not left held.
The reason to re-task an agent is that the fleet needs it elsewhere, so leaving it holding a
cluster it will never finish freezes those items and their areas for a full lease, which is
the starvation the divvy exists to prevent. The cost is real, so `propose_allocation` prefers
idle agents, and the Fleet view's Apply names what will be released before you commit. A held
agent is re-taskable, never silently.

`claim_next` / `claim_review` / `claim_cluster` gain an optional `wait_seconds` so a poll is
one blocking call rather than a spin loop. Empty queue returns
`{claimed: false, fleet_idle: true}` and the role prompt says **stop** — an idle agent must
not burn tokens waiting.

### D-f. A bounced item goes back to its author first

A reviewer's `bounce` returns the item to `next`, but **pinned to the agent that built it for
one lease period**. That agent still has the worktree, the branch, and the context; handing
half-finished work to a cold agent throws away exactly what cluster assignment exists to
preserve. After one lease period unclaimed — the author was re-tasked, or died like AGT-5 in
the mock — the pin lapses and the item opens to the fleet.

A hard author-only pin was the tempting version and it is wrong: it strands the item when
the author never comes back, which is the common case rather than the exotic one.

### D-g. Fleet credentials are ephemeral by default

A dashboard that mints keys must also retire them, or a month of waves leaves a drawer of
live long-lived credentials scoped to write. Fleet keys therefore default to
`expires_at = 24h` and carry `roles` narrowed to the one role they were minted for. `ApiKey`
already has `expires_at` and `revoked` (AL-72) — this is a default and a UI, not a new
mechanism. The Fleet view lists every key it issued with its agent and offers **End wave**:
revoke every fleet key, mark every agent `offline`, release outstanding leases and
reservations.

## 4. Roles

| Role | Can | Cannot |
|---|---|---|
| **planner** | `create_prd` · `grill_prd` · `decompose_prd` · `prd_coverage` · `propose_allocation` · `assign_role` · `fleet_status` · `collision_clusters` | `claim_next` · `claim_cluster` — **the orchestrator plans; it does not quietly do the work** |
| **worker** | `claim_next` · `claim_cluster` · `heartbeat` · `update_item` (status ceiling `review`) · `release_item` · `add_memory` · all reads | set `status: done` · `sign_off` · any PRD write · `assign_role` |
| **reviewer** | `claim_review` (only where `claimed_by != self`) · `sign_off` (→ `done`) · `bounce` (→ `next`, reason required) · all reads | `claim_next` · `claim_cluster` · `update_item` on item bodies |

`planner` is a superset of nothing on purpose. If the human wants to build in their own
window they mint a second, worker-eligible key for it — visible in `fleet_status` as an
agent, subject to the same review gate as any other worker.

## 5. Data model

```
Agent                      NEW
  id                       AGT-n, per-project sequence (PRD-13 key convention)
  project_id               FK
  api_key_id               FK — the credential, hence the eligible roles
  label                    "claude-opus-5 @ macbook:wt-2"
  active_role              planner | worker | reviewer
  role_assigned_at         set by assign_role
  role_acked_at            set when a poll response delivered the directive (§D-e).
                           assigned > acked  ⇒  next response carries the directive.
                           No queue table — the comparison IS the outbox.
  capabilities             JSON: {vendor, model, tier, readonly, host}
  worktree, branch         where this agent's edits land
  branch_orphaned          set when presence lapses while a branch is unmerged. Surfaced in
                           the roster, not a footnote: an agent that died holding a branch
                           leaves state the fleet cannot clean up and the human must see.
  state                    idle | working | reviewing | offline
  registered_at, last_seen_at

ApiKey.roles               NEW  JSON list — eligible roles (the ceiling)
ApiKey.fleet_wave          NEW  nullable — tags keys the Fleet view issued, so "End wave"
                                can revoke exactly them and nothing a human minted by hand
Item.branch                NEW  the branch the work landed on; travels to the reviewer
Item.reviewed_by           NEW  the agent that signed off — never equal to claimed_by
Item.bounce_pinned_to      NEW  agent the bounce returned it to (§D-f)
Item.bounce_pinned_until   NEW  when the pin lapses and the fleet may take it
AreaReservation            NEW  agent_id, area, item_id, expires_at (in-flight touch-areas)
```

`Agent.last_seen_at` is presence and reuses the existing lease vocabulary: `heartbeat`
extends *both* the item lease and the agent's presence. An agent past its presence TTL is
`offline`, its item leases lapse into the existing stale-reclaim path
(`items.py:_is_claimable`), and its reservations expire. **Agent death needs no new
mechanism** — it is the lease timeout that already ships.

**Presence TTL is derived, not its own constant: `lease_seconds / 4`, with agents
heartbeating at TTL/3.** One clock governs item leases, reservation horizons, the bounce-pin
duration, and presence together, so a project that raises `lease_seconds` for long builds
automatically gets a longer presence window. An independent constant would silently declare
healthy workers dead mid-edit on exactly those projects. The 3× gap between heartbeat and TTL
absorbs network latency — an agent must miss three consecutive heartbeats to be declared
offline. It is deliberately **not** per-agent configurable: a fleet where agents disagree
about what "alive" means makes the roster's one job unanswerable.

**Agent ids come from a per-project sequence** (PRD-13 convention), so simultaneous
registrations on one key get distinct ids by construction. Duplicate *labels* are explicitly
allowed — two identical Claude Code windows on one machine is a legitimate fleet shape, and
de-duplicating by label would merge two real agents into one, which is the bug this PRD
exists to fix.

## 6. Slices

Each is independently shippable. **D1–D3 is a working two-role fleet; D5 is what makes it
usable.** Without the Fleet view every wave costs a trip through Settings and three
hand-assembled pastes per terminal, which is the tax that stops anyone from actually running
four agents. Consider pulling D5 forward ahead of D4 if the first wave matters more than
throughput.

### D1 — Agent registry + presence
`register_agent(label, capabilities, worktree, branch, role_hint) → {agent_id, active_role}`
· `fleet_status() → agents[], their roles, holdings, and last_seen` · `heartbeat` extends
presence · presence TTL → `offline`.

**Accept:** two terminals on the same API key register as two distinct agents with distinct
ids. Killing one flips it to `offline` within the TTL and returns its items to the queue.

### D2 — Role eligibility + the call gate
`ApiKey.roles` (defaulting existing keys to all three, so nothing in flight breaks) ·
per-tool role requirements · the `forbidden` gate of §D-b with its hint · every refusal
recorded to the audit ledger (`services/events.py`).

**Accept:** a worker-role agent calling `update_item(status="done")` gets an `unauthorized`
tool error naming the role required; the item stays `review`. The refusal appears in the
ledger with the agent id and the human principal behind the key (AL-197) — and the principal
is stamped server-side from the key, never from anything the client sends, so a compromised
client still produces a correctly attributed trail.

### D3 — `claim_review` and the self-review ban
`claim_review(agent_id) → {item, branch, touchpoints, worker_agent}` leasing an item in
`review` **where `claimed_by != caller`** · `sign_off(item, evidence)` → `done` ·
`bounce(item, reason)` → `next`, pinned to the author for one lease period (§D-f), reason on
the item and in the ledger.

**Accept:** the only agent in the fleet cannot review its own item —
`claim_review` returns `{claimed: false, reason: "no item awaiting a second pair of eyes"}`.
With two agents, A's item is reviewable by B and never by A. `Item.reviewed_by != claimed_by`
holds for every `done` item, asserted in a test. A bounced item is invisible to other workers
until its pin lapses, then claimable by any of them.

### D4 — The divvy over MCP
Expose `collision_clusters` as an MCP tool (it is REST-only today,
`routers/items.py:67`) · `claim_cluster(agent_id, max_items)` leasing a whole non-colliding
component and writing its `AreaReservation`s · hand-out checked against in-flight
reservations (§D-d) · workers write back **actual** `touchpoints` on completion, replacing
the prediction and sharpening the next round (the AL-201 capture loop).

**Accept:** with three workers registered and a backlog of overlapping items, no two
concurrently-held clusters share a touch-area. A fourth worker with no non-colliding cluster
available gets `{claimed: false, held_by: [agent...], reason: "...held by A2 — the earliest frees in 412s"}` — naming the holder and the wait, because "collides with in-flight work" is equally true of an abandoned lease
rather than a colliding one.

### D5 — The Fleet view (dashboard v1)
*Needs D1 + D2.* A new left-nav view, built on the existing dashboard/settings patterns.

**Roster.** One row per agent: id, label (`claude-opus-5 @ macbook:wt-2`), active role,
state, what it holds, worktree/branch, last seen. Offline agents fade rather than vanish, and
an orphaned branch is flagged in the row (`branch_orphaned`) — the fleet released the *item*
automatically, but the *branch* is state only a human can resolve.

**Role colour is the status that role produces** — planner purple, worker
`--color-st-in_progress`, reviewer `--color-st-review`. The roster then rhymes with the
tracker instead of teaching a fourth colour vocabulary.

**Onboard an agent.** Pick a role → pick the client (Claude Code / Codex / Cursor / Grok
Build / opencode — the list already in [mcp.md](mcp.md)) → get three copy buttons:

| | What it gives |
|---|---|
| **1. Key** | A fresh key, `roles` narrowed to the chosen role, 24h expiry, tagged to this wave (§D-f). Plaintext shown once, as today. |
| **2. Connect** | That client's exact MCP config with the key already in it — the generator that Settings → API Keys already ships, per AL-78's snippet machinery. |
| **3. Prime** | The role prompt from `gen_subagents.py` (§7), rendered for that client. |

Paste 2 into the config, paste 3 into the terminal. That is the whole onboarding.

**Review queue.** Items in `review` with their author — and the ban rendered **as a
negative on the item**: *"AGT-4 built it"* in the blocked colour, rather than a list of who
is eligible. The refusal belongs to the item, not to the roster, and stating it that way is
what makes the invariant legible at a glance.

**Collision clusters.** Each cluster's areas, who reserved it, and — for a held-back
cluster — **why**: *"collides with cluster A on `backend/app/models/`, queued until AGT-2
releases."* Without the reason a human overrides the divvy; with it they trust it.

**End wave — a hard stop, behind a confirm.** Revoke this wave's keys, release every lease
and reservation, drop pending directives, mark every agent offline, immediately. The confirm
names the damage before acting ("Revoke 4 keys, release 3 leases?"). End wave means the wave
is over; a half-ended wave with live leases is the confusing state. A key revoked with an
un-acked directive simply drops it — an un-collected directive is never assumed delivered, so
there is nothing to reconcile.

**Accept:** a human with an empty fleet stands up a worker on a second machine using only
this view — no visit to Settings, no hand-edited config. The roster shows it within one
heartbeat. **End wave** revokes exactly the keys this view issued and leaves hand-minted
keys untouched.

### D6 — Allocation + the directive downlink
`propose_allocation() → {workers: n, reviewers: n, mapping: [{agent, cluster}], rationale}`
computed from live agents + ready clusters · `assign_role(agent_id, role)` commits it · the
`directive` envelope of §D-e on every fleet tool response · re-proposal when an agent joins
or drops. **The server proposes, the planner commits** — whether the planner is the human
clicking in the Fleet view or the orchestrator agent calling the tool. Both paths write the
same row.

Dashboard v2 rides here: the proposal rendered as a diff against the current roster, with
per-agent **Apply**, and each agent's directive shown as pending until its next poll acks it
— so the human can see that a reassignment has been *issued* but not yet *collected*.

**Accept:** 4 agents / 12 ready items in 3 non-colliding clusters proposes 3 workers + 1
reviewer with a cluster each. Drop a worker → the next `propose_allocation` reflects 3.
Adding a fifth agent with no free cluster proposes it as a second reviewer, not a fourth
worker. Re-tasking a live worker to reviewer takes effect **on that agent's next poll**,
with no reconnect and no re-prime; the Fleet view shows the directive pending, then acked.

### D7 — `wait_seconds` long-poll
Optional blocking on `claim_next` / `claim_cluster` / `claim_review`, bounded (≤60s) and
holding no DB session while parked. A parked agent must still collect a directive promptly —
an outstanding directive wakes the park early rather than waiting out the timeout.

**Accept:** a parked worker returns within `wait_seconds` of an item becoming ready, and a
60-second park costs one tool call rather than twelve. `assign_role` against a parked agent
returns the directive in seconds, not at timeout.

## 7. D8 — Client half: extend the roster generator

`scripts/gen_subagents.py` already emits role prompts natively for Cursor, Claude Code, and
Codex from one `ROSTER` (AL-213). Add three **fleet** roles to that roster — `gb-orchestrator`,
`gb-worker`, `gb-reviewer` — each primed on its loop:

```
worker:     register → claim_cluster(wait=60) → build in my worktree
            → record actual touchpoints → status=review → repeat
            → queue empty: STOP, report, do not spin
reviewer:   register → claim_review(wait=60) → check out the branch it names
            → sign_off | bounce(reason) → repeat
orchestrator: register(planner) → decompose_prd → propose_allocation
            → assign_role × N → watch fleet_status → adjudicate bounces
```

Same generator, same `--check` staleness gate in CI, three more files per toolchain. The
share pack in `cursor-commands` can then ship thin `/graphban-worker` and
`/graphban-reviewer` slash commands that just invoke these.

## 8. D9 — The adversarial evidence gate

*Needs D3. Builds on AL-321 (`sabotage` evidence kind).*

**Reviewer and adversary are different jobs and must not be one habit.** A reviewer
*converges* — the job is a verdict, the queue is three deep, and an agent that blocks
everything is a bad reviewer. An adversary *diverges* — the job is one more failure mode, and
finding nothing is failure. Merge them and the convergent incentive wins under queue pressure,
which is the audit pack's self-congratulation problem moved one seat over.

The fix is **not** a fourth fleet role. A fourth terminal competes for the one resource
already named as the ceiling — the human's attention — and `cursor-commands` shows the better
shape: `bug_hunt` and `claim_bust` run as two cheap subagents dispatched by one orchestrator,
which then adjudicates. Adversarial multiplicity does not have to be fleet-level.

So make it a **precondition, not a practice**: `sign_off` refuses without adversarial
evidence, and the reviewer satisfies it however it likes — subagents with opposing lenses, or
its own passes. Same move as everywhere else here: convert a hoped-for behaviour into
something the server checks.

**Why AL-321 unblocks now.** That item is parked on a sound argument — *"a gate nobody
satisfies is a gate people route around"* (the AL-96 trust failure), plus not knowing whether
Graphban's users do mutation testing at all. That reasoning is about **human users nobody can
compel**, and it does not transfer to a fleet: a reviewer's prompt is generated by
`gen_subagents.py`, so the population that must satisfy the gate is a population we author.
The unknown that justified deferring is, here, a variable we control. Its own framing already
fits — Graphban owns the **receipt**, not the run; the receipt is self-reported, which makes
the claim *falsifiable* rather than *true*, the same trade PRD-12 accepts for citations.

**Gate on `effort`, not universally.** An adversarial pass on a one-line fix is pure tax; the
audit pack caps `quick` mode at two rounds for the same reason. Below a configurable
threshold, agent-distinct review is sufficient on its own.

**Accept:** `sign_off` on an above-threshold item without adversarial evidence is refused,
naming what is missing. A reviewer that dispatches two opposing-lens critics and records their
receipts passes. A below-threshold item signs off without one. The refusal is in the ledger.

## 9. Relationship to in-session orchestrators

Cursor 3 ships an **orchestrator pattern** of its own: a parent agent coordinating specialist
subagents — planner → implementer → verifier — each with its own context window, handing off
via structured output, defined in `.cursor/agents/*.md` or inline as `AgentDefinition`, and
isolated with `--worktree`. Claude Code has a comparable shape. This section states how PRD-17
relates to them, because "is this an alternative?" is the first question anyone familiar with
those will ask, and the answer changes what we build.

### They operate at different layers

| | In-session orchestrator | This PRD |
|---|---|---|
| Scope | one parent, one session, one vendor | N processes, many vendors, many terminals |
| Lifetime | the parent's turn | durable — leases and watermarks outlive any agent |
| Arbitration | the parent decides; it is a call tree | server-side, `UPDATE … WHERE claimed_by IS NULL` |
| Memory | context windows and handoffs | Postgres |
| Death | parent dies, children die with it | lease lapses, work returns to the pool |

A Cursor parent agent cannot see the Claude Code window beside it. That is not a deficiency to
be fixed — a call tree structurally *cannot* arbitrate between two processes that share no
parent. **An in-session orchestrator orchestrates within a session; Graphban arbitrates across
sessions.**

So this is not an alternative. It is **nesting**: an orchestrator becomes one *worker* in the
fleet, which internally fans out to its own subagents. Graphban never sees inside one and must
not try to.

### The enforcement seam

`AgentDefinition` carries an **`mcpServers`** field — per-subagent MCP server access — and the
Cursor CLI has `Mcp(server:tool)` permission patterns. That is where D2 lands, and it is the
one thing this PRD gives an in-session orchestrator that the orchestrator cannot give itself:

> Its subagent roles are **advisory** — a prompt, which a model that decides otherwise simply
> ignores. Ours are **enforced** — a property of the credential.

A `.cursor/agents/reviewer.md` saying "you are a reviewer" is a suggestion. The same definition
carrying a **role-scoped Graphban key** makes `claim_review` return `unauthorized` for anything
not reviewer-eligible. D8 (§7) already emits `.cursor/agents/`; emitting a per-agent
`mcpServers` block with the right key is a small extension with a disproportionate effect, and
it is the concrete form G2 takes on this client.

**One invariant needed work, and the draft was wrong about it (GRPH-361).** This section
originally claimed the separation fell out for free: *"a subagent shares its parent's identity,
so an in-session verifier subagent structurally cannot satisfy the reviewer gate."* It does
not. `register_agent` mints a row per call — correctly, since "two terminals on one key are two
agents" is the bug D1 exists to fix — so a subagent that registers becomes a **sibling** with
its own id, and it reviewed and signed its parent's work in a reproduction.

Identity was the wrong lever: collapsing parent and child onto `(api_key_id, host)` would undo
D1 and leave the server unable to arbitrate between two legitimate terminals. So independence
is now asked as a **separate question from identity**, only at review — a declared
`parent_agent_id` in either direction, or the same credential AND the same host. The second
also catches something this draft missed entirely: two windows of one model on one machine
sharing one key are two agents by D1's definition and are not two opinions.

With that, the claim holds as written: the verifier stays a convergent self-check inside one
author's turn, our reviewer stays adversarial and cross-agent, and the two cannot be mistaken
for one another. It is enforced rather than assumed.

### What we supply that they lack

- **A safe fan-out set.** An orchestrator can run subagents in parallel but has nothing telling
  it *which work is safe to parallelize* — it fans out on task description. `next_cluster` (D4)
  computes non-colliding clusters from actual touch-area overlap. Handed a cluster, its
  parallelism stops being a guess. This is the highest-value integration after key scoping, and
  it is already filed as the Cursor execution-backend adapter (PRD-11 §D4 / AL-215).
- **Touchpoint ground truth, flowing back.** A worktree gives a clean diff boundary, so "which
  files did this run actually modify" is nearly free. Actual touchpoints replace predictions
  (D4), which is what makes clusters sharpen over time instead of depending on hand-maintained
  `touchpoints` fields.
- **Durable memory across sessions.** Subagents get fresh context windows every time. The
  PRD-16 corpus is what one could load instead of starting cold.
- **Peers from other vendors.** The reviewer-vendor-diversity preference in D3 needs a fleet
  that is not all one vendor, which an in-session orchestrator cannot assemble by definition.

Conversely, hooks are the signal flowing the other way: `.cursor/hooks.json` already warns on
edits outside the claimed item's touchpoints, and that is where the non-compliance signal
behind quarantine comes from. MCP alone cannot observe it.

### Two places the fit is awkward, stated as constraints

**The directive downlink does not reach mid-turn.** D6 rides allocation on poll responses, and
an agent inside an orchestrated turn runs to completion rather than polling. A directive
therefore reaches it at **claim** time and not before. So an orchestrator-hosted member is a
deliberately **coarse** participant: claim a cluster, work it, report, exit. Re-tasking one
mid-flight is out of scope, and building a mechanism that silently no-ops for this client would
be worse than not having one.

**`wait_seconds` is wrong for per-run-billed cloud agents.** D7's long-poll assumes a terminal
sitting idle at negligible cost. A cloud agent billed per run that long-polls is paying to
sleep. The long-poll is opt-in per agent, never a property of the protocol.

### Why this is the durable position

A vendor's incentive is to make **its own** orchestrator good. Cross-vendor arbitration runs
against that interest — no vendor has a reason to make running two competitors in the next two
windows pleasant. The space that stays open is therefore narrower than "orchestration" and more
defensible: the **multi-vendor, durable, evidence-carrying substrate** underneath whichever
orchestrator each vendor ships.

That is the framing for this whole PRD. Graphban is not competing to be the orchestrator. It is
what lets *any* orchestrator — Cursor's, Claude Code's, or a human in a terminal — participate
in shared work without colliding. PRD-11 §D4 is consequently not a separate integration but the
**first proof** of this PRD: the case where the worker happens to be an orchestrator itself.

## 10. Non-goals

- **Orchestrating inside a session.** Graphban does not decompose a claimed cluster into
  subagents, choose models for them, or sequence their handoffs — that is the in-session
  orchestrator's job and it is better at it (§9). We arbitrate *between* sessions and stop at
  the process boundary. Reaching inside would mean reimplementing a call tree each vendor
  already ships, badly, and per vendor.
- **Spawning, waking, or killing agent processes.** The human opens the terminals; the Fleet
  view makes that one paste (§D-e). A *running* agent can be re-tasked via the directive
  downlink — a stopped one cannot be started by anything here.
- **Touching git at all.** Graphban does not clone, fetch, merge, delete branches, or open
  PRs. `worktree` and `branch` are strings an agent reports about itself, and
  `branch_orphaned` is informational — reconciling a branch that was merged elsewhere is
  entirely manual. Automating it needs repository access Graphban does not have and should
  not acquire; the tracker would become a git client, which is a different product.
- **Cross-repo or cross-project fleets.** One project, one repo, one wave. Agents do not
  persist across waves either — End wave retires them, and there is no agent history and no
  reputation.
- **Reviewers building.** A reviewer gets one item and a branch to read, never a cluster and
  never a worktree assignment.
- **A fourth `adversary` fleet role.** Considered and rejected in D9: the adversarial function
  is real, but it belongs in a `sign_off` precondition the reviewer satisfies with subagents,
  not in a terminal that competes for the human's attention.
- **Client-side reservations.** `collision_clusters` is advisory to clients; only the server
  writes reservations, because a client-enforced invariant is not an invariant.
- **Fleets beyond ~8 workers.** Collision clusters on a single repo run out well before
  that, and the human adjudicating bounces saturates earlier still.
- **Trimming the MCP manifest per active role.** Needs SSE on `/api/mcp` first (§3, D-b).

## 11. Risks and open questions

| Risk | Mitigation |
|---|---|
| **Human attention is the real ceiling.** Three workers is comfortable; at six the planner is the bottleneck adjudicating bounces. | The reviewer role absorbs the first pass — that is most of its value. Cap the proposal at a configurable `max_workers` (default 4) rather than pretending it scales. |
| **Predicted touch-areas are wrong**, so a "non-colliding" pair collides anyway. | Reservations are advisory-plus-audited: a collision that happens anyway is recorded with both agent ids so the learned model sharpens. Actual touchpoints always replace predictions (D4). |
| **Idle agents burn tokens.** | `wait_seconds` + an explicit `fleet_idle` signal + STOP in the role prompt (§7). |
| **A worker abandons mid-lease with a dirty worktree.** | Lease lapses, item returns to `next`, `AreaReservation` expires — but the branch survives. `Agent.branch_orphaned` flags it in the roster row; cleanup is the human's. |
| **`roles` defaulting for existing keys.** | Default to all three so no in-flight integration breaks; the fleet UI nudges toward narrowing. Documented in the migration. |
| **A key-minting dashboard sprays credentials.** Every wave leaves live write-scoped keys behind. | 24h expiry + single-role `roles` + wave tagging + **End wave** (§D-f). The Fleet view is the only place that issues them, so it is also the only place that has to remember them. |
| **A directive is issued to an agent that never polls again.** | It stays pending and visibly un-acked in the Fleet view; presence TTL flips the agent `offline` and the next `propose_allocation` reallocates its work. An un-collected directive is never assumed delivered. |

**No open questions remain.** The draft's one open item — the presence TTL — was closed
during the grill: it is derived as `lease_seconds / 4` rather than being its own constant
(see Data model). The grill also forced two decisions the draft had not reached, both now
recorded above: what `assign_role` does to an agent that is holding work, and what happens to
an agent that persistently ignores its directive.

## 12. Acceptance walk

Following the PRD-14/15 convention ([acceptance-prd15.md](acceptance-prd15.md)) — run against
a real stack in an isolated compose project, not asserted in unit tests.

Four terminals, stood up **only from the Fleet view**: one planner (Claude Code), two workers
(Codex, Cursor) in separate worktrees, one reviewer (Grok Build). Planner decomposes a PRD,
proposes an allocation, commits it; workers build their clusters concurrently; every item
passes through the reviewer.

| # | Step | Expected |
|---|---|---|
| 1 | Onboard 4 agents via the Fleet view | 4 keys issued, each single-role, 24h expiry, wave-tagged |
| 2 | Each terminal registers | Roster shows 4 agents, distinct ids, correct roles |
| 3 | Worker calls `update_item(status="done")` | `unauthorized` naming `reviewer`; item stays `review`; refusal in the ledger with the human principal |
| 4 | Two workers claim concurrently | Zero shared touch-areas between the held clusters |
| 5 | Worker tries `claim_review` on its own item | Refused — no self-review |
| 6 | Reviewer signs off | `reviewed_by != claimed_by` on every `done` item; where the fleet has the diversity, `reviewer.vendor != author.vendor` |
| 6b | Reviewer signs off an above-threshold item with no adversarial receipt | Refused, naming what is missing; passes once two opposing-lens critics are recorded |
| 7 | **Promote a worker to reviewer while it holds its own item in `review`** | **Both gates refuse it** — `claim_review` filters it out, and `sign_off` would fail its `reviewed_by != claimed_by` assertion. A role change cannot launder authorship |
| 8 | Re-task a live worker → reviewer | Takes effect on its next poll; no reconnect; directive shown pending, then acked; its held items returned to `next` |
| 9 | Reviewer bounces an item | Returns to `next`, invisible to other workers until the pin lapses, then claimable by any |
| 10 | Kill a worker mid-lease | `offline` within TTL; item back to `next`; reservation expired; orphaned branch surfaced |
| 11 | **End wave** | This wave's keys revoked; leases and reservations released; a hand-minted key untouched |
