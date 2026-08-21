# PRD-22 — Fleet supervisor: a local CLI that spawns and retires fleet members

**Ledger id:** GRPH-P22
**Status:** approved — EARNED by completing the grill on 2026-08-21; all four dimensions
(`contracts`, `failure_modes`, `open_decisions`, `scope_edges`) graded `resolved` across six
turns. `approved` is not settable by hand.
**Depends on:** PRD-17 (fleet roles) · PRD-19 (enrolment sessions) · GRPH-361 (parentage) ·
AL-201 (worktree spike) · AL-192 (collision clustering)

> **On the number.** Numbering is per-project and enforced (`services/keys.py:mint` takes
> `max(number)` within the project; `uq_prd_number` is the backstop). Graphban's PRD sequence
> is *sparse* — 2, 3, 4, 6 and 8 went to other projects while numbering was still global, and
> migration 0038 deliberately preserved each row's existing digits rather than renumbering
> ("the gaps a shared counter left behind are preserved — sparse history is accurate"). So the
> next number is whatever the ledger issues, never one past the highest `docs/prd-*.md`
> filename. This PRD was drafted as "PRD-26" from those filenames and renumbered (GRPH-425).

## 1. Overview

PRD-17 §D-e settled the transport question honestly: MCP is client→server, the server cannot
wake an idle terminal, so **the human is the uplink** — a person opens a terminal and pastes.
That is still true and this PRD does not contest it. What it observes is narrower: the human
is not the only thing on the developer's machine that can open a terminal. A local process can,
and a local process is the one component in the system with a filesystem, a git binary, a
process table, and the vendor CLIs installed.

So the missing piece is not a protocol. It is a **supervisor**: a thin client that runs where
the agents actually run, spawns them holding server-issued seats, and reaps them.

Three things already in the tree only make sense if this was coming:

- **`mint_enrolment_as`** (`services/fleet.py:1454`) says it outright — *"A planner mints a
  seat for an agent it is about to spawn... the capability has to exist for an autonomous
  fleet to be possible at all."* Planner-only, bounded by the minter's credential ceiling.
- **`independent()`** (`services/fleet.py:695`) accepts *one credential, two seats* as genuine
  independence, so one key can provision a whole fleet and review inside it still means
  something.
- **`mint_enrolment_as` deliberately does not set parentage**, because siblings under one
  parent are one call tree and every seat a planner issued would be mutually non-independent.

Spin-up is therefore already agent-callable. The server half is done. This PRD builds the
process that consumes it, and closes the spin-**down** asymmetry it exposes (§6).

### What this is not

**The server still never spawns anything, and still never touches git.** Both remain PRD-17
§10 non-goals and this PRD does not weaken them — it satisfies them from the other side of
the process boundary. The supervisor holds no authority: it can only launch a process holding
a seat the *server* issued, to do work the *server* arbitrates. Delete the supervisor and
every invariant still holds; the fleet just needs a human to open terminals again.

It is also not an in-session orchestrator (PRD-17 §9). It does not decompose work, choose
models for subagents, or sequence handoffs. It starts processes and stops them.

## 2. Goals

- **G1** — A planner can bring a worker into existence without a human pasting a code.
- **G2** — The supervisor carries **no authority of its own**. Every capability it exercises
  is one the server granted to a credential it was handed.
- **G3** — A spawned pair is **independent by construction**: one seat each, no declared
  parentage, verified by a test that fails if either changes.
- **G4** — Spin-down is as available as spin-up. A fleet that can only grow is not a fleet.
- **G5** — Vendor diversity becomes **enforceable** rather than merely preferred, because the
  supervisor is the only component that chooses which binary runs.
- **G6** — Install surface is httpx-thin. No fastapi, no sqlalchemy, no pgvector on a laptop.
- **G7** — The client↔server contract cannot drift silently.

## 3. Key decisions

### D-a. Two servers, and only one of them has authority

**Arbitration is remote and authoritative; process control is local and unprivileged.** That
sentence is the architecture, and it is stated before the diagram because the grill misread it
three times — repeatedly asking for the "HTTP routes" and "endpoint URLs" by which the
supervisor would invoke `spawn` on the Graphban server. There are none. If a careful reader
inverts this twice, the document was at fault.

The planner attaches two MCP servers:

```
planner (a terminal, or an in-session orchestrator)
 ├─ graphban   (remote HTTP)  → mint_enrolment, propose_allocation, assign_role, fleet_status
 └─ gbfleet    (local stdio)  → spawn, stop, ps, orphans
```

`gbfleet` runs on the developer's machine and the Graphban server never learns its calls
happened. Authentication on the local surface is **process ownership** — the planner speaks
over a pipe to a child it launched — not a credential. `spawn` takes a seat it cannot mint and
a cluster it cannot assign; it is a launcher, and a launcher that lies gets a process the
server refuses.

**Errors on the local surface are JSON-RPC tool results carrying `isError`**, with the message
in `content` — the same shape the Graphban server uses, and the same shape `gen_prd_index.py`
had to learn when a tool-level failure arrived inside `result` rather than as a JSON-RPC error.
There are no HTTP status codes anywhere in this surface. A child that exits non-zero before
registering produces a tool error naming the adapter, the exit code, and the last lines of
stderr — never an exception reaching the planner as a transport failure, because the planner
must be able to tell *your adapter is broken* from *the supervisor is gone*.

### D-b. A spawned child never declares parentage

The intuitive implementation — *I spawned it, therefore `parent_agent_id = me`* — silently
disables review across the entire fleet, because `independent()` treats siblings under one
parent as one call tree. The spawned child is a **separate process**, not a subagent inside
the planner's turn, and parentage means the latter. It is left unset, exactly as
`mint_enrolment_as` leaves it, and `minted_by` carries the audit trail instead.

This is the defect class this repo keeps meeting: it would not error, it would not fail a
test written the obvious way, and the fleet would read as correctly provisioned while review
inside it had quietly stopped meaning anything. GRPH-361 is the same bug from the other
direction.

**The test is specified here rather than left to the implementer, because the obvious version
is vacuous:**

- Assert `independent(a, b)` **and** `independent(b, a)` for a spawned pair — distinct seats,
  no declared parentage. Both directions, because the predicate checks parentage both ways and
  a one-directional test passes with half the gate deleted.
- **Each with a control** that sets `parent_agent_id` and asserts the result flips. Without the
  control the pair is independent for other reasons too, so the assertion passes even when
  parentage is being set — which is precisely what GRPH-361 was sent back for.
- The registration payload **omits the key entirely** rather than sending null, and a test
  asserts absence, so a refactor cannot reintroduce it as a default that reads as intentional.

Pure unit tests: no processes, no ledger, no network. They run in the normal suite on both
engines.

### D-c. Spawn on demand; do not long-poll

PRD-17 §9 already states `wait_seconds` is wrong for per-run-billed agents — a run that
long-polls is paying to sleep. A spawned worker is exactly that shape, so it takes
`wait_seconds=0`, works what it claims, and **exits on empty**.

The idling moves to the supervisor, which is a few MB of Python and free. This inverts the
terminal design and fits better: the supervisor watches `propose_allocation`, spawns when
there is non-colliding work, and lets processes die when there is not. **Exit is the normal
end of a worker's life, not a failure.**

### D-d. Spin-down is a kill, not a directive — and revocation reaches it through the planner

PRD-17 §9's other constraint: the directive downlink does not reach an agent mid-turn, and a
headless run is one long turn. Re-tasking a running spawned worker is therefore **out of
scope** — building a mechanism that silently no-ops for this client would be worse than not
having one. A spawned member is deliberately coarse: claim → build → report → exit.

**The supervisor kills in exactly four cases**: an explicit `stop`; the lease deadline reached
with no successful heartbeat (D-i); `--max-workers` lowered; supervisor shutdown. Notably
**not** `fleet_idle` — the worker exits itself on empty per D-c, and two things owning one
transition is how they come to disagree about it. Propagation is SIGTERM, then SIGKILL after a
grace period. **Killing never cleans up**: the worktree is left exactly as it is, and salvage
happens on reap (D-g).

**Revocation mid-execution needs a watcher, and the planner is it.** `end_wave` and
`retire_wave` revoke a seat while a child is still building. There is no push channel — PRD-17
§D-e, unchanged — so the child discovers it only on its next server call, and a child deep in
a build may not make one for a long time.

The planner closes this **without a new transport, because the planner is a client of both
servers**. It already polls Graphban; when it sees a seat revoked or an agent gone from the
roster it calls `stop` on the local `gbfleet` with a reason. That is PRD-17 §D-e's "the human
is the uplink" with the planner in the human's place.

The supervisor keeps a **backstop poll** regardless, because a planner that is idle, dead, or
mid-turn notifies nobody, and *"End wave is a hard stop"* is only true if something is
watching. Primary path: planner notification, which is prompt. Backstop: the supervisor's own
poll, bounded by its interval. Two paths to the same transition is fine here — `stop` is
idempotent — where two paths to *deciding* the transition would not be.

### D-e. Same repo, separate distribution

New top-level `fleet/` with its own `pyproject.toml`, publishing `graphban-fleet` with a
`gbfleet` entry point. Not a second repo, and not inside `backend/`.

**Not a second repo,** because the contract has no schema anywhere: the enrolment code format
and its 30-minute TTL, `register_agent`'s return shape, `independent()`'s seat semantics, the
directive envelope, `fleet_idle`. All of it is changeable in a single PR here, and a
cross-repo break would present as absence reading clean — the fleet still spawns, nothing
errors, review stops being independent. The precedent is also already set: the client half of
the fleet lives here today, in `scripts/gen_subagents.py` with a `--check` staleness gate in
CI. And the load-bearing test (D-b) cannot be written anywhere the fleet service is not.

**Not inside `backend/`,** because `graphban-api` pulls fastapi, sqlalchemy, pgvector, psycopg,
alembic, redis and cryptography, and a laptop needs none of them. `graphban` and `agentledger`
are both already claimed as console scripts (`backend/pyproject.toml:25`), so a distinct entry
point is required regardless.

### D-f. The seat reaches a child as a file, and the format is per-adapter

There is no single JSON here. It is each vendor's MCP config, and even the mechanism for
getting the path to the child differs:

| Vendor | Path mechanism |
| --- | --- |
| Claude Code | `--mcp-config <path>` — a private temp file works |
| Cursor | no per-invocation flag; reads `.cursor/mcp.json` from the **project directory** |
| Codex | its own config path, per adapter |

**Cursor is the constrained case and it resolves through the worktree.** Two Cursor children
sharing one config would redeem the same seat and the second would fail on single-use. But
each child's worktree *is* its own project directory, so `.cursor/mcp.json` written inside the
worktree is already per-child. The catch: `.cursor/` is a **tracked path in this repo**, so the
supervisor would be writing a live credential into a tracked location. It must be gitignored
inside the worktree, and §9 asserts the file is gone after reap.

**The file cannot be protected from the child, and does not need to be.** The child runs as the
same user and can edit anything it is given. It gains nothing: the seat is single-use and
already redeemed at `register_agent`, and the server decides the role from the **seat**, not
from the config on disk. Tampering breaks only the child's own connection. **The file is
transport; the server-side enrolment is the artifact** — any design leaning on the file's
integrity is leaning on the wrong object.

Written chmod 600, removed when the child is reaped, never inside the repo except where a
vendor forces it, and never committed.

### D-g. The worktree belongs to the supervisor — salvage on reap, adjudicate on resume

One worker, one worktree, one branch — created and removed by the supervisor. Graphban keeps
its hands clean: `worktree` and `branch` stay self-reported strings, `branch_orphaned` stays
informational, and PRD-17's "touching git at all" non-goal binds the *server*. The supervisor
is a client and may run git; that is most of why it exists.

**"Dirty" means `git status --porcelain` is non-empty, including untracked files.** Not
pedantry: untracked is exactly where the seat file lives. A definition covering only tracked
modifications would call a worktree clean while a live credential sat in it.

**The reason never to delete a dirty worktree is that uncommitted work is unrecoverable. So
commit it, and that reason evaporates.**

- **The supervisor never judges content.** At reap it classifies: clean → remove; dirty →
  **salvage**, a WIP commit on the worker's own branch, after which the worktree is removed.
  Nothing is destroyed, the work is in git and recoverable indefinitely, and the cost is a
  branch rather than a working tree. Disk stops growing without anything deleting work.
- **Resume is the only judgment, and it is the planner's.** The supervisor knows which orphaned
  branches map to which items and whether those items are still open — that list is mechanical
  and it produces it (`gbfleet orphans`). It cannot know whether a half-finished diff is worth
  continuing. Resuming an item another agent has already rebuilt is how two divergent solutions
  appear, so the condition is that the item is still open and unclaimed. The supervisor offers;
  it does not decide.
- **Never delete on a timer, in any form.** A timer that removes uncommitted work is the tool
  this PRD refused, running slower.

**Salvage must not commit the credential.** D-f writes a live seat into the worktree, and for
Cursor that is inside the project directory. Salvage gitignores-and-verifies **before**
committing. This is a stronger requirement than "unlink on reap", because reap is precisely
what does not happen when a worker dies — the salvage path exists only for the case where the
tidy path was skipped. §9 asserts it.

**Branch naming.** One worker owns a cluster, not an item, so the branch is named after the
agent: `gb/<wave>-<agent-short-id>`. Deterministic, collision-free by construction, and it
survives the cluster changing underneath it. If the branch already exists, **refuse to spawn
and say so** — never force, never auto-suffix, because both silently attach a worker to
somebody else's history.

### D-h. One supervisor per repo, enforced by a lockfile

A lockfile in the supervisor's state dir, keyed on the repo path. A second supervisor for the
same repo refuses to start, or attaches read-only. Supervisors on different repos never
contend.

This is what makes **`--max-workers` locally correct** rather than locally approximate. An
earlier draft accepted the hole — two supervisors on one host exceeding the cap between them —
and the hole closes for free, because the cap's natural scope *is* that repo's worktree pool.
It also answers duplicate worktrees and double-spawn, both of which required two supervisors to
exist.

**The lock checks pid liveness, not mere presence.** A reboot otherwise leaves a lock nobody
holds and the next supervisor refuses to start forever.

**A supervisor crash does not kill its children** — they are separate processes. So a new
supervisor **adopts** them from the recoverable state written under its temp dir, rather than
starting blind beside a fleet it does not know about. That state is on disk deliberately: a
supervisor crash should cost the supervisor, not the fleet. On reboot the children are gone
too, so recovery there is only the stale lock plus orphaned worktrees, which salvage handles.

### D-i. Offline: the lease decides

The instinct is for the supervisor to keep running offline until claimed work is finished. It
cannot reach `done`: `sign_off` and `bounce` are server acts, and leases expire server-side
because heartbeats cannot land. Worse, the unbounded version puts **two agents on one item**
the moment the partition is one-sided — the laptop is offline, the server is fine and re-hands
the item — which is the collision that clustering exists to prevent.

**Until a worker's lease expires the server will not give its item to anyone else**, so a child
that cannot reach the server may keep building until its own deadline and not past it. That is
not optimism; it is what the lease promises.

- The supervisor tracks each child's deadline locally (`register_agent` returns
  `heartbeat_interval_seconds`; `lease_seconds` is known at claim) and keeps retrying.
- At the deadline with no successful heartbeat it **stops that child**, leaving worktree and
  branch intact. The work survives; the claim does not.
- **No new spawns while the server is unreachable** — a child that cannot register has no
  identity, no consumed seat and no claim.
- On reconnect **nothing is replayed blind**: re-read item state, submit transitions only for
  items still held, and report anything reclaimed via the existing `branch_orphaned` path.
- A reviewer finishing offline holds its verdict and submits on reconnect only if the item is
  still in review and it is still independent. **`sign_off` is never faked locally.**

**Stated ceiling: one lease period** of useful partition tolerance, not unbounded operation.
The knob already exists — raise `lease_seconds`. This covers the real failures (wifi drops, a
server restart) without inventing offline consensus.

### D-j. The supervisor never decides a role

It watches queue depth and the backlog and scales the fleet — but **`propose_allocation`
already computes the mix** from live agents and free clusters, returning agents beyond the free
clusters as reviewers rather than surplus workers. A supervisor forming its own opinion makes
two allocators, and the fleet oscillates between them.

**The server computes the mix, the planner mints against it, the supervisor executes.** The
supervisor decides *how many* of an already-authorized kind to run and when to stop — scaling,
not allocation.

For the autonomous loop, waking an LLM planner to mint a seat for every scale decision is slow
and expensive for a mechanical call. The escape is S1's deterministic mode: a pool of seats
minted up front, and the supervisor picks which to redeem. **Redeeming from a pre-authorized
pool is not assigning a role** — the authority was granted at mint time.

### D-k. No sandbox, and no claim of one

Directory confinement was considered and rejected, because it breaks four things: linked
worktrees carry a `.git` file pointing at the parent object store; auth and caches live in
`$HOME` (`~/.claude.json`, `~/.cursor/mcp.json`, keychain, `uv`/`pnpm` caches); it contradicts
D-h's recoverable state in temp folders; and forcing worktrees under the repo root makes a
plain `pytest` collect every worktree's copy of the suite.

The threat model does not support it either. The supervisor runs binaries the user installed,
as the user, and its purpose is to let them write code and run the toolchain — which *is* the
dangerous capability, so a sandbox permitting it permits what matters.

**What is claimed instead, and only this:** the child's cwd is its own worktree, so a bad worker
cannot reach another's files; the per-child vendor permission config the supervisor already
writes; and `.cursor/hooks.json` warning on edits outside the claimed item's touchpoints.
§7 carries the matching non-goal. Naming it "sandboxed" when it is confined by convention is
this repo's recurring defect aimed at a security boundary, and someone would rely on it. On
macOS there is no cheap primitive for the stronger version anyway (`sandbox-exec` is
deprecated), so the weaker claim is also the accurate one.

**Stated trigger for revisiting:** spawning agents to run untrusted or generated code, or
running on a shared machine.

## 4. Authority: what the supervisor may and may not do

| | |
|---|---|
| May | create/remove/salvage worktrees, exec vendor binaries, write per-child config, kill children, read `propose_allocation` / `fleet_status`, decide **how many** children to run |
| May not | mint a seat, **decide a role**, assign a role, claim work, sign off, bounce, decide independence, judge whether a diff is worth resuming |
| Holds | whatever credential the human gave it, and nothing more |

The test of this table: **compromise the supervisor and what is the worst outcome?** It can
launch processes holding seats it was already given, on the machine it already runs on. It
cannot manufacture authority, promote anything, or pass work — the credential ceiling
(`eligible_roles`) still binds, and a planner still cannot build. It is **not** a security
boundary (D-k, §7).

## 5. Slices

- **S1 — `gbfleet up`, deterministic.** No LLM in the loop. Reads `propose_allocation`, mints
  nothing, takes pre-minted seats on the command line, spawns N children in worktrees, reaps
  them, reports. Proves spawn/reap/worktree/lock/teardown end to end. **Shippable and useful
  alone**, and if the deterministic version proves sufficient, stopping here is a real outcome.
- **S2 — Vendor adapters.** One module per binary (`claude`, `codex`, `cursor-agent`, `grok`),
  each declaring exactly four things: argv construction, config format and where it is written,
  stdout parsing, and exit-code semantics.
  **Selection is explicit, never inferred.** The spawn call names the vendor; the supervisor
  resolves the binary against a pinned supported-version range and refuses at spawn on
  mismatch. No PATH auto-detection — picking whichever binary happens to be installed produces
  a fleet whose composition nobody chose, quietly defeating G5, the one thing the supervisor is
  uniquely able to enforce.
  **A broken adapter must fail at spawn, loudly**, and must never produce a child that runs but
  never registers — the silent drop, indistinguishable from a slow start. A child that has not
  registered inside a bounded window (seconds, not the seat's 30 minutes) is killed and its
  adapter reported.
- **S3 — The stdio MCP server.** `spawn` / `stop` / `ps` / `orphans`, wrapping S1's functions.
  The slice that makes the planner autonomous, and thin because S1 did the work.
- **S4 — Retirement, and seat visibility.** §6.
- **S5 — Touchpoint capture.** The worktree gives a clean diff boundary, so *which files did
  this run actually modify* is nearly free. Write it back as `touchpoints`. Phase 1 of the
  AL-201 spike, which called it the highest-value lowest-risk slice on its own.
- **S6 — Observability.** Structured lines on stdout and a rotating file in the state dir —
  **never a telemetry endpoint**, which would put a network dependency in the component whose
  job is to keep working when the network is gone. Per child: adapter and resolved binary
  version, seat id (never the code), worktree path, branch, pid, **registration latency**, exit
  code, and reap disposition (clean / salvaged / left dirty). Registration latency is the
  load-bearing field: a child that never registers is the silent-drop failure, and this is what
  distinguishes it from a slow start.
- **S7 — Packaging, licence, CI.** A `fleet/**` entry in `dorny/paths-filter`, publish workflow,
  and the licence decision in §8.

## 6. The teardown asymmetry — mint, list and retire are one capability

Spin-up is agent-callable; spin-down is not. `issue_seats`, `dismiss_agent`, `end_wave` and
`revoke_expired_keys` all sit behind `Depends(get_current_user)` (`routers/fleet.py:110,182,218`).
A planner can mint a seat for an agent it is about to spawn and then can neither retire it nor
**see what became of it**: there is no `list_enrolments` over MCP, seat state
(`unused`/`consumed`/`expired`/`revoked`) is derived server-side and exposed only over REST
behind user auth, and `fleet_status` carries per-agent `enrolled` as a bare boolean — the
consequences of revocation, never the transition.

That was coherent while a human opened every terminal. It stops being coherent the moment a
planner provisions its own fleet, and it fails in the direction that costs money: a fleet that
can grow and not shrink.

**Resolution: mint, list and retire are one capability with one scope — `minted_by`**, which
already records the provenance needed to bound it. The containment argument is the same one
`mint_enrolment_as` already makes: a planner cannot build, so it has no authored work that
seeing or revoking its own seats could launder.

### `retire_wave` — the contract

- **Scope: seats where `minted_by` is the caller, and nothing else.** It cannot reach another
  planner's seats, and cannot touch hand-minted long-lived credentials — `end_wave` already
  restricts itself to `fleet_wave`-tagged keys, because revoking somebody's personal key would
  be a surprise the button never promised.
- **Effect: revoke those seats and release the leases and reservations held by agents on them,
  in one transaction.** A half-retired wave — seats dead, leases held — is the genuinely
  confusing state: work no living agent can finish, held by credentials that no longer
  authenticate.
- **It does not stop processes, and must not appear to.** The server has no process control;
  that is this PRD's premise, not an omission. Termination follows because the planner then
  calls the supervisor's `stop` (D-d), or the backstop poll notices. **`retire_wave` is a
  credential operation; `stop` is a process operation.** Conflating them lets a planner call
  `retire_wave`, see success, and leave four agents building against revoked seats.
- **`list_enrolments`, planner-scoped by `minted_by`**, so the planner can see seat state
  rather than infer it from an agent vanishing.
- **Human `end_wave` stays and stays broader** — every fleet key regardless of minter. The
  planner gains a scoped subset, not a replacement, and the UI hard-stop does not go away.

## 7. Non-goals

- **Spawning from the server.** Unchanged from PRD-17 §10. The server has no filesystem, no
  git, and no business acquiring either.
- **Being a security boundary.** D-k. A compromised vendor binary has whatever the user has.
  The worktree is a blast-radius convention, not a sandbox.
- **Claiming a spend ceiling it cannot enforce.** The supervisor cannot see token spend —
  vendors report usage inconsistently and some not at all in headless output. It enforces what
  it can measure (max concurrent workers, max wall-clock per child, max children per wave) and
  reports whatever each adapter can parse, per adapter. A budget guardrail people rely on and
  which silently does not bind is worse than none, because the reliance is what causes the
  spend.
- **Re-tasking a running spawned worker.** D-d. Kill it and spawn what you needed.
- **Deciding roles or the fleet mix.** D-j.
- **Orchestrating inside a session.** Still the in-session orchestrator's job, still better at
  it (PRD-17 §9). An in-session orchestrator running as a worker is a black box: the supervisor
  starts it, waits, reaps it, and never reaches inside.
- **Remote spawn.** The supervisor spawns on the machine it runs on. Starting processes on
  other hosts makes it a scheduler — a different product with a different threat model.
- **Cross-repo or cross-project fleets.** One repo, one project, one supervisor, enforced by
  D-h's lock.
- **Merging, pushing, or opening PRs.** Salvage commits stay local. Publishing is the human's
  act.
- **Cleaning up dirty worktrees on a timer.** D-g. Salvaged, never deleted blind.
- **Replacing the Fleet view.** The human uplink stays; the supervisor is inert in single-agent
  posture and adds nothing there. Both paths produce identical roster rows.

## 8. Risks and open questions

| Risk | Mitigation |
|---|---|
| **Parentage set on spawned children** — silently ends independent review fleet-wide | D-b, with both-direction assertions and controls that fail if parentage is introduced |
| **Autonomous provisioning crosses PRD-17 §D-e** — issuing a credential and admitting an agent were named as decisions that *"should never be automatic"* | `mint_enrolment_as` already took the first half, deliberately, with a containment argument. This takes the second. Containment: local host only, one supervisor per repo, wave-scoped, `--max-workers`, ceiling still binds, everything dies with the supervisor. **The change the grill should attack hardest** |
| **Cost.** A supervisor that spawns on a loop can spend real money while nobody watches | Measurable limits only (§7), plus `fleet_idle` meaning stop. An autonomous fleet needs a budget before it needs features |
| **Blast radius.** Headless runs mean weakened permission prompts | The worktree boundary plus `.cursor/hooks.json`, which already warns on edits outside the claimed item's touchpoints. Hooks are the only signal MCP cannot observe. Explicitly not a sandbox (D-k) |
| **A salvage commit containing a live credential** | D-g gitignores-and-verifies before committing; §9 asserts it |
| **Vendor contract churn.** Four headless CLIs, one of them 0.1 beta | S2's adapters, pinned version ranges, refusal at spawn, and a documented support matrix |
| **Seat on disk.** D-f writes a live credential to a file | 30-minute TTL, single use, chmod 600, unlink on reap, gitignored where a vendor forces it into the tree. Bounded, not eliminated |
| **Contract drift** between supervisor and server | Same repo (D-e), plus a contract test in the backend suite |
| **Human attention is still the ceiling** (PRD-17's own risk table) | Removing the human from *spawn* does not remove them from *bounce adjudication* and *resume*. `max_workers` default stays 4 |

**Open — the licence.** FSL-1.1's Competing Use clause is comfortable for a supervisor that is
inert without a Graphban server. But the contributions this component most wants are **vendor
adapters**, from people who use other tools, and FSL is friction for exactly that audience. A
per-directory `fleet/LICENSE` at Apache-2.0 answers it without a second repo. Not decided.

**Open — the name.** `graphban-fleet` / `gbfleet`. PyPI availability unverified.

**Stated split trigger.** The one condition that would justify extracting this to its own
repository: outside contributors on vendor adapters who should not hold commit access to the
server. If that day comes, extract **the adapter interface only**, not the supervisor.

## 9. Acceptance walk

1. Planner registers on a planner seat; `claim_next` is refused. *(unchanged, PRD-17)*
2. Planner calls `mint_enrolment` twice — one worker seat, one reviewer seat.
3. Planner calls local `spawn` twice. Two processes exist, two worktrees exist, two agents
   appear in `fleet_status` with distinct ids and distinct `enrolment_id`s.
4. **Neither spawned agent has a `parent_agent_id` key at all**, and `independent()` is True in
   **both** directions.
5. A second supervisor on the same repo refuses to start (D-h), naming the holder's pid.
6. Worker claims a cluster, builds, moves it to review, exits. `ps` shows it gone; the roster
   shows it offline within the presence TTL without anything reporting the death.
7. Reviewer claims that item — permitted, because §4 held — signs off or bounces, exits.
8. **After reap, the child's seat file is gone**, including the Cursor case inside the worktree.
9. A worker is killed mid-build with uncommitted changes. Its lease lapses, its item returns to
   `next`, reservations drop, and the tree is **salvaged**: a WIP commit on its own branch,
   worktree removed, **and the commit contains no credential**.
10. `gbfleet orphans` lists the salvaged branch against its still-open item; the planner
    chooses resume or leave. The supervisor never chooses.
11. Adapter version mismatch refuses **at spawn**, naming the binary and the supported range.
12. A child that never registers is killed inside the registration window and its adapter
    named — not left waiting on a lease that will never exist.
13. Server made unreachable mid-build: children keep working, no new spawns, each stops at its
    own lease deadline. On reconnect, transitions replay only for items still held; anything
    reclaimed is reported, never overwritten.
14. `retire_wave` revokes only the caller's minted seats, releases their leases, and **does not
    stop any process** — the planner's subsequent `stop` does that.
15. `reissue_enrolment` replaces a dead seat and a fresh worker picks the item up.
16. Retirement: the fleet shrinks to zero without a human touching the Fleet view, and End wave
    in the UI still works and still hard-stops everything.
17. Real data: run it against this repo's own backlog and read what it actually did — which
    files each worker touched versus what the cluster predicted.
