# Two axes, not five words: collapsing the fleet's role and supervision surface

**Ledger id:** GRPH-P39 — created in the ledger 2026-09-05, approved v1.0 2026-09-06. The ledger
is the source of truth; this file is a review copy and must agree with it
(`backend/tests/test_prd_sync.py`). It is untracked: committing it means regenerating
`docs/prd-index.json` with `scripts/gen_prd_index.py`, or
`test_the_snapshot_accounts_for_every_repo_prd` fails.

**Status:** approved — v1.0, 2026-09-06, after one grill round of eight questions, three of
which changed decisions. v0.1 carried a worker-side fleet-wide exit test, a `bounce_count` column
and an `api_keys.roles` data migration; v0.2 cut all three and §4 D-i records why the first was
wrong. This revision adds D-j (the merge has a precondition v0.2 did not see), rewrites D-d (the
axis it was migrating into is dead), gives D-i the dependency it leaned on without naming, and
re-orders the slices so the supervision axis — where the tokens are — ships before the merge.
**Depends on:** PRD-17 (roles, `independent()`, the authorship ban) · PRD-19 (enrolment seats) ·
PRD-22 (the supervisor and its authority table) · PRD-24 (gbagent) · PRD-35/36 (delegation,
`spawn(tier)`) · PRD-37 (the preference matrix) · GRPH-543 (`gate` follows the reviewer role) ·
the unattended-wave draft (P30, `until`)
**Verified against the tree:** 2026-09-05, `main` @ `5a13176d`; the grill findings were read
against the same commit on 2026-09-06.
**Touches:** `backend/app/services/fleet.py` (`ROLES`, `TOOL_ROLES`, `eligible_roles`,
`register_agent`'s seat branch, the argument ceiling, `mint_fleet_key`, `propose_allocation`) ·
`backend/app/mcp_server.py` (the attestation gate, `_with_attestation`, tool descriptions and
one reply field) · `backend/app/security/authz.py` (`key_gate_ids`) ·
`backend/app/services/fleet_profiles.py` · `backend/app/routers/fleet.py` ·
`fleet/src/gbfleet/matrix.py` + `matrix.toml` · `fleet/src/gbfleet/{mcp,seat,until,doctor}.py` ·
`fleet/src/gbagent/` (the loop that consumes the seat instruction) ·
`web/src/features/fleet/{wave.ts,FleetView.tsx}` · `web/src/lib/graph/presence.ts` ·
`docs/prd-17-fleet-roles.md`, `docs/fleet-adapters.md`, `docs/mcp.md`, `AGENTS.md`.
**No migration.** D-b resolves legacy role values on read. That is not a convenience: it makes
the change reversible, leaves hosted orgs' credentials untouched, and removes the one step of
this design that could not be rolled back. The grill found the one read site v0.2 missed —
`register_agent` copies `seat.role` into a fresh agent without looking at it — and D-b now
covers it.

---

## 1. Overview

<!-- framing -->

An operator standing up a fleet today has to hold five words at once: **planner**, **worker**,
**reviewer**, **supervisor**, **all-in-one** — plus a second vocabulary of **cheap** and
**frontier**, plus a matrix whose rows are keyed by harness × model × lane × role × tier. Asked
"what do I run, and on what model", there is no short answer, and the long answer is spread
across a server enum, a local process, a TOML table and two PRDs.

The five words are not five things. They are **two axes wearing one vocabulary**:

- **Authority** — what the *server* will let a caller do. Enforced, keyed on a credential
  ceiling and a registered agent's active role.
- **Supervision** — whether an *LLM* sits in the spawn-and-adjudicate loop, or whether a
  deterministic local process runs the wave. Not enforced by anything, because it is a
  property of a process on someone's laptop, not of a permission.

This PRD states them as two axes with two values each and deletes the words that are on
neither. Authority becomes `planner | worker`; `reviewer` merges into `worker`. Supervision
becomes `deterministic | driven`, which is a name for two modes that already ship.
`all-in-one` survives unchanged as the solo posture.

### The load-bearing observation

**The self-review ban was never held up by the `reviewer` role.** It is keyed on authorship at
two independent sites — the `claim_review` filter `it.built_by != agent_id`
(`services/fleet.py:1311`) and the assertion at `sign_off` (`services/fleet.py:1420`) — and
PRD-17 says why there are two: *"a single gate keyed on a query is one refactor away from being
keyed on the caller's current role instead of on authorship."*

So merging `worker` and `reviewer` costs no invariant **that is keyed on authorship** — and the
grill found exactly one that is keyed on the role instead: the `gate` scope, which decides who
may write an attestation (§2.6). D-j moves it onto authorship, the same key the other two sites
use, and it is a precondition of the merge rather than a consequence of it. With D-j in place
the sentence holds without the qualifier. Two things in the tree already say so.
`propose_allocation` documents the merged posture at `services/fleet.py:2725` — *"an
all-in-one agent files into the review pool and pulls from it like every other posture, and
both independence gates already govern the outcome, so N of them review each other."* And
`items.py:1314` records that the two holds were deliberately kept in separate columns because
*"a reviewer holding a review claim may still be a worker elsewhere (GRPH-429)"* — one agent
holding a build lease and a review claim at the same time is not a new state this PRD invents;
it is a state the server was built to allow and no role was ever able to occupy.

### The operating model this produces

A supervisor is given **X seats to provision**. Each worker runs one loop: **try review, else
build, else exit.** Bounces return to the pool and are picked up by whichever worker is free.
The wave ends when the backlog is empty and everything built has been signed.

Three of the four moving parts already exist and are not rebuilt here. `bounce` already returns
an item to `next` pinned to its author for one lease period, and the pin **lapses** into general
redistribution (`services/fleet.py:1553`, `1729`). `until` already mints when there is review
work outstanding and already computes the fleet-wide facts that end a wave (`until.py:296-311`,
`376`, `404`). What is missing is one line: the seat instruction currently says the *opposite*
of the loop above — *"claim work with claim_cluster (wait_seconds=0) and EXIT when there is
nothing to claim"* (`fleet/src/gbfleet/seat.py:111`).

### What this is not

- **Not a weakening of review.** G3 of PRD-17 stands verbatim: an item cannot reach `done`
  without a second agent's verdict. D-c shows the merge *tightens* the path to `done`, and D-j
  extends the same authorship key to attestation.
- **Not a new supervisor.** The supervisor keeps its two-read allowlist
  (`fleet/src/gbfleet/client.py:43`) and mints nothing.
- **Not a model router.** Which model runs which lane stays PRD-37's question, resolved on the
  machine. This PRD only removes the `role` axis from the matrix's key, and one duplicated
  policy with it (D-d).
- **Not a change to the solo posture.** A lone agent still registers, still marks its own work
  done, and PRD-17's argument that this is *correct* rather than a hole is untouched.
- **Not a new scheduler.** D-i is a decision NOT to build one.

---

## 2. Problem

<!-- framing -->

Every fact below was read from the tree at `5a13176d`.

### 2.1 Three roles, two of which differ only by which queue they pull from

`ROLES = ("planner", "worker", "reviewer")` (`services/fleet.py:27`). Strip the authorship ban
— which is keyed on identity, not role — and what is left distinguishing `worker` from
`reviewer` is: `claim_next`/`next_cluster`/`claim_cluster` are worker-gated,
`claim_review`/`sign_off`/`bounce` are reviewer-gated (`services/fleet.py:648-670`). Both pull
from a queue; the ban decides what each may take out of it either way.

The stated reason for keeping them apart is at `services/fleet.py:664`: *"A reviewer that could
claim fresh work would drift into being a worker holding review authority, which is self-review
with extra steps."* That is not what happens. An agent holding both would build item A and
review item B — the ban still refuses it item A. It is two jobs on two items, which is the
thing `items.py:1314` says the columns were split to permit.

### 2.2 A dedicated reviewer is an idle context, and the wave loop has a workaround for it

The unattended-wave draft records the failure mode directly: *"Reviewers leave before there is
anything to review… The same `up` starts worker and reviewer seats together. Reviewers
`claim_review`, get nothing, exit. Workers later move items to `review` with nobody left to
sign them."*

`until.py` carries the compensation: a separate reviewer mint path, `max_reviewers`,
`_need_reviewer(...)`, and a `REVIEWER_FAILS` counter that stops minting reviewers after
consecutive register/claim failures on a non-empty queue (`until.py:57`, `274-295`, `376-378`).
Every line of that exists to schedule a role that cannot do the other job while it waits.

### 2.3 The token cost is in supervision, and supervision is not a role

Two modes ship and neither is named:

- **Deterministic.** `gbfleet up` — *"one wave, deterministically, with no LLM in the loop"*
  (`fleet/src/gbfleet/supervisor.py:1`). Pre-minted seats in, children out. Supervision costs
  **zero tokens**.
- **Driven.** `gbfleet mcp` attaches as a local stdio server beside the remote one, and the
  planner holds both (`fleet/src/gbfleet/mcp.py:4`): remote `mint_enrolment` /
  `propose_allocation` / `assign_role` / `fleet_status`, local `spawn` / `stop`.
  *"Authentication is process ownership."* Supervision costs a frontier context that polls
  child state and adjudicates bounces.

PRD-22's own risk table says where the ceiling really is (`supervisor.py:47`): `max_workers`
stays at 4 because *"removing the human from spawn does not remove them from bounce
adjudication and resume."* That is the expensive loop, and no amount of role arithmetic touches
it. An operator who says "the fleet wastes tokens" is describing this and has no word for it.

### 2.4 The matrix keys on an axis the server owns, and the axis it could use instead is dead

`fleet/src/gbfleet/matrix.py:34` declares `ROLES = ("worker", "reviewer")` and `resolve()` takes
`role` as part of the row key (`matrix.py:199-215`). `matrix.toml` carries ten rows split across
the two. So a server-side authority enum is a dimension of a client-side model-choice table, and
changing one requires editing the other.

v0.2 proposed moving the reviewer rows into the matrix's other axis, `LANES = ("frontend",
"backend", "mixed", "any")` (`matrix.py:33`). The grill asked what a legacy row would resolve
to, and the answer from the file is that **every one of the ten rows is `lane = "any"`**. The
lane axis has never carried a value; the role axis carries seven `worker` rows and two
`reviewer` rows and is the one in use. Migrating the live axis into the dead one would have been
work in the wrong direction. What the two reviewer rows actually say is `tier = "frontier"`,
which is an axis the matrix already has and D-f already uses.

The one policy that reads the matrix role — `reviewer_cross_vendor` (`matrix.py:325`) — is a
client-side copy of a preference the server already applies: `claim_review` prefers a
cross-vendor candidate and falls back when the fleet lacks the diversity
(`services/fleet.py:1327`). Two copies of one policy is the shape where they disagree.

### 2.5 The one thing that genuinely differs is minting, and the docs do not say so

`mint_enrolment` is planner-only, and the argument at `services/fleet.py:674` is the strongest
in the file: a worker that could mint *"would build an item, mint itself a reviewer seat,
register as a fresh agent — new id, new enrolment, therefore independent — and sign off its own
work, invisibly to an authorship ban keyed on agent id."* Planners are refused `claim_next`, so
*"a planner has NO AUTHORED WORK TO LAUNDER."*

This is the real planner/worker line. Nothing operator-facing says it; it appears only in a
comment above a dict.

### 2.6 The `gate` scope is the third site, and the only one keyed on the role

Completion needs an `attestation`, and only a `gate`-scoped key may write one
(`mcp_server.py:2078`). `authz.py:158` states the purpose: *"an ordinary agent key does not
carry it, so an agent cannot manufacture the proof that its own work is finished."*
`mint_fleet_key` grants `gate` to `reviewer` and `all-in-one`, and refuses it to `worker`
(`services/fleet.py:2086`), with the reason beside it: *"handing it the means to attest would
return the capability the role gate removes."*

So the ban has three sites, not two. `claim_review` and `sign_off` are keyed on authorship, and
survive any role change. `gate` is keyed on the role, and does not: merge the roles and either
every builder can attest (the scope's stated purpose is gone) or no fleet agent can (work parks
in `review`, which is the failure `test_the_capability_the_hint_used_to_cost_is_kept` was
written against). v0.2 said the merge costs no invariant. It costs this one, unless `gate` is
re-keyed first — which is D-j.

---

## 3. Goals

- **G1** — An operator can answer "what do I run and on what model" in two sentences.
- **G2** — Every invariant PRD-17 §G1–G7 states survives verbatim, and each is re-proved
  against the merged role rather than assumed.
- **G3** — `done` is reachable only through a path that checks authorship, and so is an
  attestation.
- **G4** — The two supervision modes have names, and the cheap one is the default.
- **G5** — Existing credentials, seats and matrix rows keep working, and the change is
  reversible without a data restore.
- **G6** — The mint argument (§2.5) is stated where an operator choosing a posture will read it.

### Non-goals

- Changing what a supervisor may call. `ALLOWED_TOOLS` stays two reads.
- Changing `independent()`, vendor diversity, or `allow_self_review`.
- Any server-side spawning. PRD-17 §10 non-goals stand.
- Retiring `all-in-one`.
- Counting bounces. Real gap, older than this change, and its own ticket (§8).
- Any claim of a manifest saving (§8).
- Adding a `review` lane to the matrix. v0.2 proposed it; §2.4 says why not.

---

## 4. Key decisions

<!-- framing -->

### D-a. The authority axis is `planner | worker`; `reviewer` merges into `worker`

`ROLES` becomes `("planner", "worker")`. `TOOL_ROLES` moves `claim_review`, `sign_off` and
`bounce` from `("reviewer",)` to `("worker",)`. `release_item` collapses from
`("worker", "reviewer")` to `("worker",)`.

The merged role keeps the name **`worker`**, deliberately, for two reasons. The first is that
the argument ceiling is keyed on that exact string (D-c). The second is naming hygiene: the repo
already has a *supervisor*, and it is authority-free and modelless. A second "supervisor" that
held review authority and ran on a model would be one word with opposite properties, which is
the drift `test_wire_name_compat.py` exists to punish elsewhere.

**D-a depends on D-j.** Until attestation is keyed on authorship, the merge hands every builder
the `gate` scope or hands it to nobody; neither is acceptable (§2.6).

### D-b. `reviewer` resolves to `worker` on READ; nothing is migrated

`ApiKey.roles` is a ceiling, not an assignment (`services/fleet.py:203`). Keys and seats in the
wild carry `"reviewer"`, and enrolment codes live 30 minutes — long enough to span a deploy. So
`eligible_roles` maps a stored `reviewer` to `worker` when it reads, and `register_agent`
accepts `role_hint="reviewer"` and resolves it the same way.

**Stored values resolve; requested values are refused.** The grill asked whether a client
sending `role="reviewer"` is normalised or rejected, and the answer is already in the tree:
`assign_role` and `mint_fleet_key` both raise `unknown role` for anything outside
`ROLES + (ALL_IN_ONE,)` (`services/fleet.py:2790`, `2072`), and resolving on read does not
soften that — `eligible_roles` narrows a *ceiling*, it does not define membership. `role_hint`
is the one request that resolves silently, and it already does today, for the reason in its own
docstring: it is a preference from a config file, and refusing a registration over a preference
strands an agent. An instruction (`assign_role`, `mint_enrolment`) is refused; a preference
(`role_hint`) is clamped; a stored fact (a key's ceiling, a seat's role) is read through. Nothing
is silently downgraded that was ever explicitly asked for.

**The seat is a stored fact too, and v0.2 missed it.** `register_agent` copies `seat.role` into
the new agent's `active_role` without checking it against `ROLES` (`services/fleet.py:254`). An
enrolment minted as `reviewer` before the deploy and redeemed after it would write a role string
the server no longer has — and `tools_off_limits` answers `role not in ROLES` with an empty
list (`services/fleet.py:975`), so the agent would be told nothing is off limits, refused on
every call, and quarantined at three (`QUARANTINE_AFTER_REFUSALS`). That is this repository's
named defect class — absence reading as clean — in the exact step that was called migration-free.
The seat branch resolves `seat.role` through the same function as the key's ceiling. One line;
a test registers on a pre-merge reviewer seat and asserts `active_role == "worker"` and a
non-empty `tools_off_limits`.

**No `api_keys.roles` rewrite, and that is a decision rather than laziness.** A rewrite is the
one step in this design with no inverse: roll back the code and every credential that used to be
a reviewer-only key is now a worker key, permanently, with nothing recording what it had been.
Resolving on read means a rollback restores the old behaviour exactly, and hosted orgs'
credentials are never touched by a deploy they did not ask for. `eligible_roles` keeps treating
an empty list as "unspecified, therefore all", never as "none".

**A narrowed reviewer credential gains authority under this change** — it can now claim fresh
work, and under D-j it keeps `gate`. That is a real widening and is stated rather than
discovered. It is accepted because the ban that matters is on authorship, because D-j makes
attestation answer to the same key, and because the alternative is a role that exists solely to
be an eligible-set value nothing assigns. It belongs in the release notes, not only in a test.

So the two claims this PRD defends, together and without contradiction: **with D-j in place the
merge costs no invariant, and it widens one credential class.**

### D-c. The argument ceiling is what keeps `done` honest, and the merge tightens it

Today `WORKER_STATUS_CEILING = "review"`, and the ceiling refuses `update_item(status="done")`
and `release_item(to_status="done")` when `role == "worker"` (`services/fleet.py:792`,
`936-955`). It is keyed on the role string, and **`reviewer` is not subject to it** — so a
reviewer today has a path to `done` through `update_item` that never reaches `sign_off` and
therefore never evaluates `built_by != caller`.

Merging into `worker` closes that path. Every merged agent is under the ceiling, so `done` is
reachable **only** via `sign_off`, which is the authorship-checked call. The merge is a
narrowing here, not a widening, and that is the answer to "did you just let workers approve
their own work": they cannot write `done` at all.

The grill asked whether the ceiling is evaluated before or after a legacy `reviewer` resolves to
`worker`. After: the resolution happens at the ceiling (`eligible_roles`) and at assignment
(`active_role`), both of which precede `role_for_call`, and the ceiling reads the resolved
string. So a merged agent on a legacy reviewer key is caught on both `update_item` and
`release_item`, which is the property the sabotage in §6 pins.

`all-in-one` stays outside the ceiling, exactly as today, and that remains the posture where the
human is the reviewer.

### D-d. The matrix loses its `role` axis and the duplicated policy that read it; no lane is added

`matrix.ROLES` is deleted, `role` leaves the row schema, and `resolve()` and `spawn`
(`fleet/src/gbfleet/mcp.py:89`) lose their `role` parameter. The key becomes harness × model ×
lane × tier. `LANES` is **not** extended: §2.4 shows the lane axis has never carried a value, and
the two rows v0.2 wanted to move into a `review` lane are already distinguished by
`tier = "frontier"`, which is what D-f keys on.

**The compat shim accepts `role=` and ignores it**, for one release, with a deprecation note in
the reply. A value that named authority carried no information the matrix can use once every
spawn is a `worker`, so there is nothing to map it to and inventing a mapping would be the
error. Nothing is orphaned: the measured-cell key is `(vendor, model, lane, tier)`
(`matrix.py:302`) and never carried `role`.

**`reviewer_cross_vendor` goes with it.** It is keyed on `role == "reviewer"`
(`matrix.py:325`), and the server's `claim_review` already prefers a cross-vendor reviewer and
falls back with a reason when the fleet lacks the diversity (`services/fleet.py:1327`). The
server copy is the stronger of the two — it sees the actual author's vendor at claim time rather
than a prediction at spawn time — and it is the one that stays. Deleting the client copy is a
simplification in the direction this PRD already argues, and the merge is what made the
duplication visible.

### D-e. The supervision axis is named, and `deterministic` is the default

| mode | what runs the wave | LLM in the loop | token cost of supervision |
|---|---|---|---|
| `deterministic` | `gbfleet up` / `until` | none | zero |
| `driven` | a planner holding the local stdio server | yes, a frontier context | the planner's window, continuously |

Both already ship; naming them is most of the work. The behavioural change is that the docs,
`doctor` and the Fleet view present `deterministic` as the default and `driven` as the
escalation, and that `driven` states what it is for: **bounce adjudication and resume**, the two
things PRD-22 says a human was never removed from.

### D-f. The model guidance is one table, and it never mentions a supervisor

Because a supervisor has no model. The whole of the guidance:

| | model |
|---|---|
| planner | frontier |
| worker | cheap |
| worker, after its second bounce on one item | frontier |

That is PRD-37's default row expressed for a human. The matrix stays where it is, for the case
where the default is wrong on a particular machine.

### D-g. `all-in-one` keeps its name, and the hazard is stated where it lives

Not "dangerous mode". PRD-17 argues that a solo agent marking its own work done is *correct* —
the review happens where it always did, a person reading the diff — and labelling the commonest
deployment dangerous tells most users they are holding it wrong.

What *is* hazardous is the combination §2.5 describes: **an agent that can both mint and build
can defeat the authorship ban by minting itself a fresh identity.** That is why `mint_enrolment`
is planner-only. v0.2 wanted that sentence beside a posture chooser, and the grill asked whether
`gbfleet` shows it too. Neither does, because there is no chooser: the Fleet view deliberately
does not offer `all-in-one` as a wave key (`FleetView.tsx:966-970`), and the posture is an
un-enrolled agent on an ordinary API key. So the sentence goes in the two places the posture is
actually chosen — the API-key mint dialog in Settings and PRD-17's role section — and nowhere a
fleet operator would look for it, because a fleet operator never chooses it.

### D-h. One loop with a priority order — review first, and no phases

The merged worker tries `claim_review`, falls through to `claim_cluster`, and exits when both
are empty. It is a priority order evaluated every iteration, **not** a build phase followed by a
review phase.

Phases would be wrong twice over. A worker that reviews and bounces has just *created* build
work, so the transition runs in both directions anyway. And `items.py:1314` says the server
already permits a build lease and a review claim to coexist on one agent, so a phase would be a
restriction this PRD invents rather than a structure it discovers.

Review comes first, and the argument is the bounce pin's own lease. An item reviewed promptly is
bounced while its author still holds the worktree, the branch and the context the pin exists to
preserve — `services/fleet.py:1557` says exactly what a late bounce throws away. Review late and
the pin has lapsed, the author has moved on, and the redistribution hands half-finished work to
a cold agent. Reviewing first is also the cheaper call and the only one that moves anything to
`done`.

**The order is a sentence in a prompt, and nothing arbitrates it.** Both claims run with
`wait_seconds=0` (`seat.py:105-125`), so neither blocks; a worker that keeps finding review work
keeps reviewing until the queue drains, and only then builds. That is starvation of the build
queue in the strict sense, and it is accepted: the review queue draining is the wave's tail and
the thing `until` waits on last, so emptying it first is the intended priority rather than a
pathology. A scheduler here would be a phase by another name. `seat.py:102` already says a
prompt is the weakest guard there is, which is why the ordering is also the cheapest thing in
this PRD to change once there is a wave to measure (§8).

`INSTRUCTION` and `REVIEWER_INSTRUCTION` (`seat.py:105`, `116`) collapse into one template,
matching the role merge.

### D-i. The end-of-wave decision stays in `until`; a worker exits on a LOCAL reading

**This is a decision not to build something, and it reverses v0.1 of this draft.** That version
had each worker consult a fleet-wide fact before exiting — review queue empty AND no other
worker holding a build — to avoid the stranded last item: worker A builds the final item, moves
it to `review`, cannot see it (the ban filters out its own work), finds both queues empty and
exits; B exited earlier for the same reason; the item is stranded.

The failure is real. Putting the fix in the worker is not, for three reasons:

1. **`until` already does it — once its trigger is re-keyed.** It mints when review work is
   outstanding (`until.py:376`) and already reads `live`, `holdings` and unsigned rows. But the
   grill found what v0.2 glossed: the backstop is built from the role D-a deletes.
   `live_reviewers`, `_need_reviewer` and the `reviewer_fails` counter are all keyed on
   `child.role == "reviewer"` (`until.py:281`, `294-295`). After the merge no child is a
   reviewer, so the counter never increments and the guard never fires. **D-i therefore depends
   on S6 re-keying that guard on the fact it was measuring:** a child was spawned against
   unheld review rows, exited, and the rows are still unheld. `REVIEWER_FAILS = 3` is already
   the max-retry the grill asked for — a wave that spawns three such children in a row exits
   `review-unsigned`, exit 1, rather than spawning a fourth — and it keeps that job under a
   name that no longer mentions a role.
2. **The worker is the worst place to define the unreachable case.** `until` has a stated rule —
   *"Unknown is not empty: leftover review must not look like idle"* (`until.py:306`). A worker
   deciding whether to exit when it cannot reach the server has to re-derive that rule at the
   exact moment it has no information, and getting it wrong strands work in one direction and
   burns tokens in the other.
3. **It would be a second scheduler.** Two components computing when a wave is finished is the
   shape where they disagree and nobody can say which is right.

**The race the grill asked about is closed for `until`'s own children.** `until` declares
`review-unsigned` only when there is no live child and no holding (`until.py:400-407`), and a
child counts as live from the moment it is spawned — before it registers, before it claims — so
a freshly spawned worker that has not yet appeared server-side cannot make the wave look idle.
An agent started by something other than `until` is invisible to it, and `until` never claimed
otherwise: it supervises the children it spawned.

**`gbfleet up` keeps failing loudly.** One-shot mode has nothing to respawn workers, so a
stranded last item still ends the wave `review-unsigned`, exit 1 (`until.py:404`). That is the
honest outcome for a mode that promises one wave and no scheduling, and the remedy is `until`,
which the exit reason should name.

**The cost, stated:** a respawn re-pays a cold start — a fresh process, registration and
manifest — where a resident worker would have paid nothing but poll calls. That is real and
accepted, because it buys a single place where "the wave is over" is decided.

### D-j. Attestation is keyed on authorship, not on the role — and this precedes the merge

`gate` stays a scope, and the merged `worker` is minted with it. What changes is the check
behind it: a write that carries an `attestation` receipt or a `head_commit`
(`mcp_server.py:2078`) is refused when the caller is a registered agent **and** is the item's
`built_by`. That is the third site of the ban, keyed on the same fact as the other two, and it
is what lets `gate` follow the merged role without giving a builder the means to attest its own
work.

Two things this does not touch. An adapter key — CI, a sync bridge — carries no agent identity,
so the authorship check has nothing to compare and the write proceeds as it does today; the
existing owner-bounded `key_gate_ids` check (`authz.py:166`) is untouched. And `all-in-one` is
outside the check exactly as it is outside the ceiling, because in that posture the human is the
reviewer and PRD-17 says so.

**A legacy reviewer key keeps `gate` across the S2/S3 window.** `gate` is written into
`key.scopes` at mint (`services/fleet.py:2086`) and `key_gate_ids` reads it from there
(`authz.py:166`); nothing derives it from `ROLES` at call time. So shrinking `ROLES` in S3
cannot strip attestation from a key that already carries it, and a reviewer registered before
the deploy goes on signing off without re-registering. The scope is a stored fact, and D-b's
rule for stored facts applies: read through, never rewritten.

**Why this is a precondition and not a slice of the merge.** Ship D-a first and there is a
window in which every fleet worker either holds `gate` with no authorship check or holds no
`gate` at all. The first is the hole `authz.py:158` exists to close; the second parks every
item in `review`. There is no order of the same release in which both are avoided, so D-j is its
own release (S2), and the merge does not start until it has landed.

**The manifest is measured, not assumed.** `gate` swaps the tool schema through
`_with_attestation` (`mcp_server.py:1747`), so every merged-worker manifest carries the
attestation shape a reviewer's does today. `test_mcp_footprint` has eight tokens of headroom.
S2 measures the merged-worker manifest before S3 begins, and if it does not fit, that is a
finding for this PRD and not a number to raise.

---

## 5. Slices

<!-- framing -->

In ship order, and each slice below is its own section so that it is its own item. **S1 — the
supervision axis** — shipped in PR #643 on 2026-09-06 (the `AGENTS.md` / `gen_subagents.py`
instruction that a backlog is a job for `until`, regenerated across the three toolchains) and is
not listed again. It was independent of everything below and was where the token cost lived.
The remaining six are the merge and its precondition; **S2 is its own release and S3 does not
start until S2 has landed** (D-j).

Every slice carries the same loop: read the decision it names in §4, write the sabotage in §6
FIRST and watch it fail, then build until it passes, on both database engines. "Should now be
rare" is not evidence anywhere in this PRD.

---

## S2 — Attestation keyed on authorship (D-j), measured before the merge

**What.** A write that carries an `attestation` receipt or a `head_commit` is refused when the
caller is a registered agent **and** is the item's `built_by`. Today the only check is the
`gate` scope on the key (`mcp_server.py:2078`, purpose at `authz.py:158`); this adds the
authorship comparison beside it, keyed on the same column `claim_review` (`fleet.py:1311`) and
`sign_off` (`fleet.py:1420`) already use. Nothing else about `gate` changes in this slice:
`mint_fleet_key` still grants it to `reviewer` and `all-in-one` only — S3 extends it to
`worker`, and that is why this must land first.

**Untouched, deliberately.** An adapter key (CI, a sync bridge) carries no agent identity, so
the comparison has nothing to compare and the write proceeds exactly as today; the owner-bounded
`key_gate_ids` check (`authz.py:166`) is not changed. `all-in-one` is outside the check as it is
outside the ceiling — in that posture the human is the reviewer (PRD-17).

**The refusal names the column.** The message says `built_by`, not "not permitted": the next
person to hit it must be able to tell an authorship refusal from a scope refusal without reading
the source.

**Measure the manifest.** `gate` swaps the tool schema through `_with_attestation`
(`mcp_server.py:1747`). After S3 every merged-worker manifest will carry that shape, and
`test_mcp_footprint` has eight tokens of headroom (`MEASURED_TOKENS` 14192 / `CEILING` 14200).
Measure a `worker` key's `tools/list` **with** `gate` now, record the number in §7.11 of this
PRD, and if it does not fit, the answer is a smaller attestation shape — not a bigger ceiling.
That finding, if it comes, blocks S3.

**Sabotage (write these first).** A `gate`-scoped registered agent calls `update_item` with an
`attestation` receipt on the item it built → refused, and the refusal text contains `built_by`.
The same agent attests an item built by another → accepted. A `gate`-scoped key with no
registered agent attests either → accepted. Delete the new comparison and confirm exactly the
first test fails.

**Acceptance:** §7.4 (the attestation half) and §7.11.

---

## S3 — The merge, server side (D-a, D-b)

**What.** `ROLES` becomes `("planner", "worker")`. `TOOL_ROLES` moves `claim_review`,
`sign_off` and `bounce` from `("reviewer",)` to `("worker",)`; `release_item` collapses to
`("worker",)`. `mint_fleet_key` grants `gate` to `worker` (S2 made that safe).
`propose_allocation` proposes only `planner` and `worker`: its "at least one reviewer as soon
as there are two agents" branch deletes, and the "one worker per free cluster" reasoning stays
and now has somewhere to put the surplus agent — the review queue. A one-seat wave is named as
the `all-in-one` posture, not as a fleet with a missing reviewer.

**Stored values resolve; requested values are refused (D-b).** `eligible_roles` maps a stored
`"reviewer"` in `ApiKey.roles` to `worker` on read, and a key stored as `["reviewer"]` must
resolve to exactly `("worker",)` — asserted explicitly, never "not empty", because the fallthrough
for an empty tuple is "all roles" and that is a silent widening. `register_agent`'s seat branch
(`fleet.py:254`) resolves `seat.role` through the same function, so a pre-merge reviewer seat
redeemed after the deploy registers as `worker`; today it copies the string unvalidated, and
`tools_off_limits` answers an unknown role with `[]` (`fleet.py:975`) — told nothing is off
limits, refused on every call, quarantined in three. `role_hint="reviewer"` clamps silently, as
any hint does today. `assign_role` and `mint_enrolment` with `role="reviewer"` are refused with
`unknown role` **and the two roles that exist** — today the message names only the bad value.

**No migration.** No `api_keys.roles` rewrite: a rewrite has no inverse, and resolving on read
makes rollback restore the old behaviour exactly. Hosted orgs' credentials are not touched.

**The named widening.** A narrowed reviewer credential can now claim fresh work and keeps
`gate`. Stated in the release notes, not only in a test.

**Sabotage.** A key stored as `["reviewer"]` → `eligible_roles` returns exactly `("worker",)`.
Redeem a seat stored as `reviewer` → `active_role == "worker"` and `tools_off_limits` is
non-empty. `assign_role(role="reviewer")` → refused, message lists `planner, worker`.
`propose_allocation` for 1, 2 and 4 agents → no `reviewer` anywhere in the mapping or the
rationale. The mint argument's guard — `mint_enrolment` stays `("planner",)` — still passes.

**Acceptance:** §7.1, §7.10.

---

**Release note (the sentence the merge commit carries).** *A credential narrowed to `reviewer`
can now claim fresh work, and keeps `gate`. `reviewer` resolves to `worker` on read; nothing
stored is rewritten, and rolling back restores the old behaviour exactly.* Release notes are
built from merge subjects since the previous CalVer (`scripts/graphban_release.py notes`), so
the S3 PR title states the widening rather than a changelog file nobody reads.

## S4 — The ceiling proof (D-c), on both engines

**What.** Tests, not code, unless a test finds something. `done` is unreachable except through
`sign_off` for every role except `all-in-one`. Today `WORKER_STATUS_CEILING = "review"` refuses
`update_item(status="done")` and `release_item(to_status="done")` when `role == "worker"`
(`fleet.py:936-955`) and **`reviewer` is exempt** — it has a path to `done` through
`update_item` that never evaluates `built_by`. After S3 every merged agent is under the ceiling,
so the merge is a narrowing here. This slice proves it rather than asserting it.

**The ordering the grill asked about.** Resolution happens at the ceiling (`eligible_roles`) and
at assignment (`active_role`), both before `role_for_call`, so the ceiling reads the resolved
string. A merged agent on a legacy reviewer key must be caught on both calls.

**Sabotage — three independent deletions, three different failures.** (1) Delete the ceiling
branch → a test fails. (2) Delete the `sign_off` authorship assert → a *different* test fails.
(3) Delete the S2 comparison → a third fails. If any two deletions fail the same test, the ban
has one site where it claims three, and that is the finding.

**Acceptance:** §7.3, §7.4 (the `done` half).

---

## S5 — The matrix loses `role` (D-d)

**What.** `matrix.ROLES` is deleted, `role` leaves the row schema in `matrix.py` and
`matrix.toml`, and `resolve()`, `spawn` (`gbfleet/mcp.py:89`) and `doctor` lose their `role`
parameter. The key becomes harness × model × lane × tier. `LANES` is **not** extended — every
one of the ten rows is `lane = "any"`, the axis has never carried a value, and the two rows
that were `role = "reviewer"` are already `tier = "frontier"`, which is what D-f keys on.

**The shim.** `role=` is accepted and ignored for one release, with a deprecation note in the
reply. There is nothing to map it to — it named authority, and every spawn is a `worker` now —
so no default is invented. Nothing is orphaned: the measured-cell key is
`(vendor, model, lane, tier)` (`matrix.py:302`) and never carried `role`.

**`reviewer_cross_vendor` goes with it.** It is keyed on `role == "reviewer"`
(`matrix.py:325`) and is a client-side copy of the server's `claim_review` preference
(`fleet.py:1327`), which sees the real author's vendor at claim time. Delete the policy, its
config key and its `doctor` line; the server copy is the whole policy from here.

**Sabotage.** Load a `matrix.toml` row carrying `role` → loads, reply carries the deprecation
note. Call `spawn(role="reviewer")` → spawns, reply carries the note, the chosen row is the one
`tier` picks. Two-vendor fleet: `claim_review` hands the item to the other vendor. Single-vendor
fleet: `claim_review` still hands it out and the reply says the preference could not be met.
`doctor` output contains no `reviewer` and no `cross_vendor`.

**Acceptance:** §7.9.

---

## S6 — One loop, and `until` re-keyed off the role (D-h, D-i)

**What, in the child.** `INSTRUCTION` and `REVIEWER_INSTRUCTION` (`seat.py:105`, `116`)
collapse into one template: try `claim_review`, fall through to `claim_cluster`, exit when both
are empty — a priority order evaluated every iteration, not phases (GRPH-429: a build lease and
a review claim may coexist on one agent). Both calls stay `wait_seconds=0`; nothing arbitrates
the order and that is accepted, because the review queue draining is the wave's tail. The
gbagent loop that consumes the instruction changes to match.

**What, in `until`.** The `review-unsigned` backstop is built from the role S3 deletes:
`live_reviewers`, `_need_reviewer` and `reviewer_fails` are all keyed on
`child.role == "reviewer"` (`until.py:281`, `294-295`), so after the merge the counter would
never increment and the wave would spawn workers forever against an item nobody can sign.
Re-key it on the fact it measures — a child was spawned against unheld review rows, exited, and
the rows are still unheld. `REVIEWER_FAILS = 3` keeps its value under a role-free name.
`max_reviewers` and the reviewer mint path go; the merged worker pulls from whichever queue
has work. `gbfleet up` keeps failing loudly on a stranded last item, exit 1, and the reason
names `until` as the remedy.

**Sabotage.** Seed a review queue longer than the build queue → the backlog still drains.
Produce the stranded item on demand (the last item built by the only remaining child) → under
`up`, `review-unsigned` exit 1 naming `until`; under `until`, a worker is spawned and signs it,
exit 0. A child that registers and exits without claiming, three times → `review-unsigned` after
exactly three spawns, not two and not four. Delete the re-keyed counter → that test fails.

**Acceptance:** §7.2, §7.5, §7.6, §7.7, §7.8.

---

## S7 — The surface and the docs for the merge (D-e, D-g)

**What.** `wave.ts` `WAVE_ROLES`, `FleetView`, `presence.ts` and the Live view lose `reviewer`;
`doctor` reports the supervision mode by name and the Fleet view presents `deterministic` as
the default and `driven` as the escalation for bounce adjudication and resume. The mint hazard
— an agent that can both mint and build defeats the authorship ban by minting itself a fresh
identity (`fleet.py:674`) — goes into the API-key mint dialog in Settings, where the
`all-in-one` posture is actually chosen, and nowhere in `gbfleet`, which cannot produce it.

**Docs.** PRD-17's role vocabulary updated in place; `docs/mcp.md`, `docs/fleet-adapters.md`,
`fleet/README.md`, `AGENTS.md` (regenerate with `scripts/gen_subagents.py`; the always-applied
Cursor rule is capped at 4000 chars). `test_docs_sync`, `test_docs_completeness` and
`test_prd_sync`'s `KNOWN_BODY_DIVERGENCE` for PRD-17 all bind here, and the frontend guards
that assert `all-in-one` is not one of the three role colours still pass with two.

**Sabotage.** `grep -rn reviewer web/src fleet/src docs/*.md AGENTS.md` returns only the
historical mentions this PRD lists, each annotated. The API-key dialog test asserts the hazard
sentence is rendered for an unnarrowed key and absent for a role-narrowed one.

**Acceptance:** §7.8.

---

## 6. Failure modes

<!-- framing -->

The shape to fear is this repository's recurring one: **a guard that stops binding without
anything going red.** Each candidate is paired with the sabotage that would catch it.

- **The ceiling stops applying.** If `eligible_roles` resolves after the ceiling check rather
  than before, an agent registered on a legacy reviewer seat keeps its exemption and the
  `update_item` path to `done` survives the merge. *Sabotage:* register on a legacy reviewer
  seat, call `update_item(status="done")`, and require a refusal.
- **`gate` widens without its check.** If S3 mints `worker` with `gate` and the D-j check is
  absent or keyed on the wrong column, every builder can attest its own item, and nothing goes
  red because attestation writes succeed. *Sabotage:* a `gate`-scoped worker calls
  `update_item` with an `attestation` receipt on the item it built, and the test requires the
  refusal to name `built_by`.
- **A dead role string reads as unrestricted.** If the seat branch of `register_agent` is left
  as it is, a pre-merge reviewer seat writes `active_role="reviewer"`, `tools_off_limits`
  returns `[]`, and the agent quarantines itself in three calls while its manifest says it may
  do anything. *Sabotage:* redeem a seat stored as `reviewer` after the merge and assert
  `active_role == "worker"` and a non-empty `tools_off_limits` — explicitly, not "registered".
- **The eligible set silently empties.** If the read-time resolution produces a value not in
  `ROLES`, `eligible_roles` filters it out and falls through to "all" — which reads as a working
  key and is a silent widening. *Sabotage:* a key stored as `["reviewer"]` must resolve to
  exactly `("worker",)`, asserted explicitly, not "not empty".
- **The cross-vendor preference has no second copy to disagree with, so its only copy must
  hold.** After D-d the server's `claim_review` preference is the whole of the policy.
  *Sabotage:* a single-vendor fleet must still hand out a review, and the reply must name the
  same-vendor fallback; a two-vendor fleet must hand the item to the other vendor first.
- **Review-first starves building.** A worker that always prefers review never builds, so a wave
  with a long review queue converges to zero throughput. *Sabotage:* seed a review queue longer
  than the build queue and assert the backlog still drains.
- **`until`'s backstop stops being reachable.** D-i leans on `review-unsigned` remaining a real
  exit, and S6 moves its trigger off the role. If the re-keyed counter is wrong, the wave spawns
  workers forever against an item nobody can sign. *Sabotage:* §7.7 — produce the stranded
  item on demand, require exit 1 after exactly `REVIEWER_FAILS` spawns, and then delete the
  counter and confirm the test fails.

And one that is not a code path: **the merge is a widening for narrowed reviewer credentials**
(D-b). Release notes, not only a test.

---

## 7. Acceptance

A walk, on a real project, against a deployed instance:

1. A planner mints two seats. `mint_enrolment(role="reviewer")` is refused with `unknown role`
   and the two roles that exist. A seat minted as `reviewer` *before* the deploy and redeemed
   after it registers as `worker`, and the reply names both what was stored and what it
   resolved to.
2. Two children register, each claims a cluster, each moves an item to `review`.
3. Each claims the *other's* item from the review queue and signs it off. Each is refused its
   own — by `claim_review`, and again by `sign_off`.
4. Neither can write `done` through `update_item` or `release_item`. Neither can write an
   `attestation` on its own item; each can on the other's. A CI key with no agent still can on
   either.
5. A worker whose build queue is empty claims and signs the other's item **in the same process**
   — the wave log shows one child doing both jobs, not a respawn.
6. A bounced item returns to `next`, is offered to its author while the pin holds, and is
   claimed by the other worker once it lapses.
7. `until` ends the wave `idle`, exit 0, everything signed. The same backlog under `gbfleet up`,
   with the last item built by the only remaining child, ends `review-unsigned`, exit 1 — and
   the reason names `until` as the remedy. Under `until` with a child that registers and exits
   without claiming, the wave ends `review-unsigned` after `REVIEWER_FAILS` spawns, not before
   and not never.
8. `gbfleet up` runs with no LLM supervising, and `doctor` reports the mode by name.
9. `claim_review` on a two-vendor fleet hands the item to the other vendor; on a single-vendor
   fleet it hands it out anyway and the reply says the preference could not be met.
10. `propose_allocation` proposes only `planner` and `worker`, and its rationale reads correctly
    for 1, 2 and 4 agents — naming a one-seat wave as the `all-in-one` posture rather than a
    fleet with a missing reviewer.
11. A merged-worker key's `tools/list` fits under `test_mcp_footprint`'s ceiling, measured, and
    the number is recorded in this PRD before S3 begins.

    **Measured 2026-09-06, S2 (GRPH-755, `gb/p39-s2-1`):** a `worker` key's manifest **with**
    `gate` is **8,159 tokens across 31 tools**; the attestation shape `_with_attestation` adds
    costs **101 tokens**; headroom under the 14,200 `CEILING` is **6,041**. `MEASURED_TOKENS`
    (14,192) is unaffected by S2, because `_with_attestation` runs at manifest time, not at
    tool definition time, so the footprint test never sees it. It fits; §8's manifest question
    is answered and S3 is not blocked on it.

    **Measured again after S3 (GRPH-756, `gb/p39-s3-1`):** `reviewer` leaving the role enums
    takes nine tokens off every manifest — `MEASURED_TOKENS` is **14,183**, headroom 17 under
    the 14,200 ceiling. §8's other prediction is now measured too: on a core-tier key a registered
    *worker* session narrows **nothing** (saved = 0 chars, asserted `== 0`), because the
    planner's tools were already tiered away and every remaining role-gated tool is the
    worker's; a *planner* session still drops all six claim/review tools, so the E9
    mechanism is idle for workers and intact for planners. `test_mcp_footprint` says both.
    A single-role fleet key's manifest saving fell with the role: a *reviewer* key saved >15%
    of the full manifest (it dropped the worker's claim tools on top of the planner's); the
    keys that exist now save **13.1% (worker)** and **12.7% (planner)**. `test_mcp_role_manifest`
    asserts a 10% floor for both; the fleet tool tier is 1,046 tokens (was 1,055).

### Walked — 2026-09-07, scratch project `p39-walk`, five runs

Every point but one is proven against the deployed instance (`1990d61a`). §7.6 — a bounce
returning to `next` pinned to its author, offered to the author while the pin holds, claimed
by the other worker once it lapses — was not exercised: no cheap reviewer bounced anything in
five runs, and contriving a failing item for a model that only signs off proves less than
running the case once gbagent's `bounce` (GRPH-778) is deployed. Deferred with that reason.

The walk found four defects older than this PRD, each masking the next, and fixed them: a
bound seat never reviewed after its build (#675); `until` spawned a child every ~14 s into a
finished project because `propose_allocation` describes the roster, not the backlog, and
`--max-children` never bound (#677); `until` read `claimed_by` — the builder's lease, still
set in `review` — as the reviewer's hold, so its review branch never fired on a real row and
a dead builder's stale hold kept a wave waiting forever (#683); and a child on a credential
spanning projects was served the key's default project's queue, not its seat's (#684). The
final run: one reviewer spawned for one unheld row, signed, `idle`, exit 0.

## 8. Open questions

- **Bounces are not counted, and under this model they circulate further.** `bounce_reason` is a
  single overwritten string (`models/__init__.py:523`) with no counter, so an item bounced five
  times is indistinguishable from one bounced once. Out of scope here — the gap predates the
  merge — but it is the first thing this change makes worse, and it wants a ticket before S6
  ships rather than after.
- **Does the manifest fit, and does it matter?** This question now has a definite input: D-j
  puts `_with_attestation` on every worker manifest, where today only a reviewer's carries it.
  Role narrowing removes only *gated* tools, so the merge may *reduce* the saving for a narrowed
  session rather than increase it. This PRD claims no saving and spends no headroom
  (`MEASURED_TOKENS` 14192 against a 14200 `CEILING`). §7.11 measures it; if it does not fit,
  the answer is a smaller attestation shape, not a bigger ceiling.
- **Should `gate` remain a scope once it is authorship-keyed?** D-j keeps it because adapter
  keys — which have no author — still need a way to say they may attest. If a later PRD gives
  adapters an identity, the scope may be redundant with the check. Not this PRD's question.
- **Does `planner` need `claim_review`?** A planner is refused `claim_next` and has no authored
  work, so letting it review is safe by the same argument that makes it safe to mint, and it
  would give a two-agent fleet a reviewer of last resort. Left out because it widens the
  credential this design leans on hardest.
- **Should `all-in-one` be derivable rather than stored?** §1 argues against two ways to say one
  thing, and this is one. Not touched because `POSTURE_SINGLE` distinguishes *chosen* from
  *merely unspecified* (`services/fleet.py:114`), which is a real distinction.
- **What happens to `STATES`?** `reviewing` stays meaningful after the role is gone
  (`services/fleet.py:132`) and probably should stay — but "the role went away and the state did
  not" is worth deciding rather than inheriting.
- **Is review-first right, or only defensible?** D-h decides it from the pin's lease, which is a
  real argument and not a universal one. It is an instruction string, so it is cheap to change
  once there is a wave to measure — which is the reason it is not a project setting today.
