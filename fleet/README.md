# graphban-fleet

The **fleet supervisor**: a thin local client that runs where the agents actually run.
It spawns vendor CLI processes holding seats the Graphban *server* issued, and it reaps
them. Ships as `graphban-fleet`, with a `gbfleet` entry point.

Specified by [PRD-22](https://github.com/asc-me/graphban/blob/main/docs/prd-22-fleet-supervisor.md).

## Two servers, and only one of them has authority

Arbitration is remote and authoritative; process control is local and unprivileged.
A planner attaches both:

```
planner (a terminal, or an in-session orchestrator)
 ├─ graphban   (remote HTTP)  → mint_enrolment, propose_allocation, assign_role, fleet_status
 └─ gbfleet    (local stdio)  → spawn, stop, ps, orphans
```

`gbfleet` runs on the developer's machine and the Graphban server never learns its calls
happened. There are **no HTTP routes on the local surface**. Authentication there is
process ownership — the planner speaks over a pipe to a child it launched — not a
credential.

**The supervisor holds no authority of its own.** It can only launch a process holding a
seat the server issued, to do work the server arbitrates. Delete it and every invariant
still holds; the fleet just needs a human to open terminals again. It is explicitly
**not a security boundary** (PRD-22 D-k): a compromised vendor binary has whatever the
user has, and the worktree is a blast-radius convention, not a sandbox.

## Why it is in this repository, and not inside `backend/`

**Not a second repository,** because the client↔server contract has no schema anywhere —
the enrolment code format and its TTL, `register_agent`'s return shape, `independent()`'s
seat semantics, the directive envelope. All of it is changeable in a single PR here, and
a cross-repo break would present as absence reading clean: the fleet still spawns,
nothing errors, review stops being independent.

**Not inside `backend/`,** because `graphban-api` pulls fastapi, sqlalchemy, pgvector,
psycopg, alembic, redis and cryptography, and a laptop running four vendor CLIs needs
none of them. `tests/test_packaging.py` derives its forbidden-dependency set from the
backend's own list, so that separation is checked rather than asserted.

## Licence — Apache-2.0, deliberately not the repository's FSL-1.1

The repository is [FSL-1.1-Apache-2.0](https://github.com/asc-me/graphban/blob/main/LICENSE.md). This directory is
[Apache-2.0](https://github.com/asc-me/graphban/blob/main/fleet/LICENSE), and the divergence is a decision (PRD-22 §8), not an oversight:

- **The supervisor is not the moat.** It is inert without a Graphban server and holds no
  authority. FSL's Competing Use clause protects the server; it protects nothing here.
- **Adapters are the contribution this component most wants**, and they come from people
  who use other vendors' tools. A non-OSI licence is friction aimed at exactly that
  audience.
- **It is a laptop-installed developer CLI**, which is precisely the kind of dependency
  that meets a corporate licence policy scanner.
- FSL-1.1-**Apache-2.0** already commits to Apache-2.0 on a two-year delay. This brings
  that grant forward for the one component least worth protecting.

If this component is ever extracted to its own repository — the stated trigger is outside
contributors who should not hold commit access to the server — extract **the adapter
interface only**, not the supervisor.

## Install

```bash
uv tool install graphban-fleet
```

That gives you `gbfleet` and `gbagent` — the coding agent is an entry point of this package
rather than one of its own, because the supervisor resolves it on PATH like any other vendor
binary. `uv tool update-shell` once if uv says its bin directory is not on your PATH, and
`uv tool upgrade graphban-fleet` to move it forward.

[`gban`](https://github.com/asc-me/graphban/blob/main/cli/README.md), the client for a human
at a terminal, is `uv tool install graphban-cli` — a separate package because it installs on
laptops that never run a wave and pulls no dependencies at all.

To run an unreleased change:

```bash
uv tool install "git+https://github.com/asc-me/graphban.git#subdirectory=fleet"
```

Releasing is [docs/releasing.md](https://github.com/asc-me/graphban/blob/main/docs/releasing.md):
the tag names the package, and the workflow refuses a tag whose version disagrees with the
pyproject.

## Running it

One wave, deterministically — you mint the seats, it spawns and reaps:

```bash
GBFLEET_API_KEY=... gbfleet up \
    --server https://cloud.agentldgr.dev --seats-file seats.txt --adapter claude
```

A seats file is one enrolment code per line. A line may also bind the seat to an item
(`WORKER-7F3K item=GRPH-755` — the child gets the BOUND instruction and claims that item
at registration, PRD-36). Both are what `until` says when it mints a seat itself; here
you say them. A mistyped token refuses the whole file before any worktree is cut, and
`doctor --seats-file` refuses it the same way.

These are the two **supervision modes** PRD-39 D-e names, and the cheap one is the default.
`deterministic` — `gbfleet up` with the seats you minted, or `gbfleet until`, which mints just
in time and stops when there is no ready work, no unsigned review and no live lease — runs the
wave with **no LLM in the loop**. `driven` is the escalation: a planner holding this local
server beside the remote one, a frontier context polling and adjudicating bounces for the
length of the wave. `doctor` prints which one you are set up for.

### Choosing what a wave works on

**`until` drains the project unless you scope it.** `--prd <id>` is the lever:

```bash
gbfleet until --repo . --server <url> --project <id> --adapter <vendor> --prd GRPH-P39
```

Without it, every ready item in the project is fair game — and **parking work in `backlog` is
not a defence**, because backlog is claimable by design so a crashed agent's item gets offered
again. There is no lever in the item shape either: `Item` has no type column, so an "epic" is a
convention in a title and invisible to the claim path. Scoping the wave is the only lever
there is (GRPH-797).

**`--prd` scopes the child's credential, not only this loop's choices.** The seat each child
registers on is minted for that PRD, and the server filters every self-claim path against it —
`claim_cluster`, `next_cluster`, `claim_next` and `claim_review` (GRPH-827). This paragraph
used to say that bound seats already achieved this. They did not, and a measured wave says how
much they did not: `--prd SA-P11` delegated three items, all inside the PRD, and the children
then self-claimed six more, none of them in it — including an ops item whose checklist mutates
production, which sits top of the queue on score. A worker declined that one on its own
judgment. Judgment is not a control, and a README claiming the containment does not create it.

Two consequences worth knowing before you use it:

- **`--prd` cannot be combined with `--seats`.** A pre-minted seat carries no scope, so the
  wave would report as scoped while those children could claim anything. Refused up front.
- **Work with no PRD is unreachable by a scoped wave**, for building and for review. Most bug
  reports have no PRD. Until a second scope axis exists (GRPH-828), the choice is a scoped
  wave or a reachable backlog, and it is yours to make knowingly.

**`--prd` refuses a server that does not support it.** An older Graphban does not reject an
unrecognised `prd_id` — it drops the property and answers the unfiltered question, so the flag
would drain the project *while reporting the wave as scoped*. Before the first spawn, `until`
asks for a PRD that cannot exist: a server that filters returns nothing, one that ignores the
argument returns the project, and the second refuses the run naming what it would otherwise
have done (GRPH-800). It then reads the tool manifest and refuses again if `delegate` takes no
`scope`, because a server can filter the divvy and still mint unscoped seats — those two halves
shipped in different releases, and passing only the first is the wave that reports as scoped
while its workers are not. Needs both server-side halves; upgrade if you see either refusal.

### What a child can reach, and what it cannot

`--strict-mcp-config` bounds the child's **tool** surface (GRPH-802). It bounds nothing else:
the child runs headless with `--dangerously-skip-permissions` and a full shell, and whatever
CLIs are on the operator's `PATH` are authenticated to whatever the operator last logged into.

So the supervisor prepends a directory of **refusing stubs** to each child's `PATH`
(GRPH-818): `railway`, `vercel`, `fly`, `heroku`, `aws`, `gcloud`, `az`, `doctl`, `kubectl`,
`helm`, `terraform`, `op`, `gh`, `psql`, `mysql`, `mongosh`, `redis-cli`. Each prints why it
refused and exits 126.

```bash
gbfleet until … --allow psql          # keep local-container verification
gbfleet until … --deny ssh            # add your own
```

`docker` is deliberately **not** denied — children verifying migrations against throwaway
Postgres containers is the evidence worth having. `gh` is denied because the supervisor opens
PRs itself now, while `gh api` is an arbitrary authenticated write to the whole forge.

**This stops an agent that wandered, not one that is trying.** An absolute path walks straight
past it, and a model that wanted to would find that in one step. The honest claim is that a
child reaching for a deployment CLI *by name* gets a refusal, and you read it in a log instead
of an incident. A sandbox is what bounds a determined process; this is not one.

### The other half: an item's prose is an instruction channel

A description is free text written by whoever filed the item, and it reaches the child as
instructions. On 2026-09-08 one item's prose asked for production work and a cheap-tier worker
did it — rotated a key, deleted rows, set environment variables on a hosted platform,
redeployed — while its four declared touchpoints were ordinary repository files, none of which
it modified. Touchpoints are a claim about FILES; there was no way to say an item reaches
outside the repository at all, so that item was structurally identical to a docs change.

Three layers now, and they are worth telling apart because only the first is a boundary:

1. **An item can declare `reach = deploy`** (GRPH-832), and one that does is refused to every
   seat, on every path — `delegate`, `claim_cluster`, `claim_next`, the bound-seat claim. Only
   a signed-in person can set or clear it: the field rides on `PATCH /api/items/{id}`, which
   takes a bearer JWT, and an agent's `update_item` refuses it outright rather than ignoring
   it. **It depends on somebody setting it**, and every item that exists defaults to `repo`.
2. **Prose that reads like deployment work costs the delegator one deliberate argument.**
   `delegate` quotes the offending lines back and refuses until `acknowledge_reach=true`,
   which is recorded against the delegation. This is a heuristic and deliberately not a
   boundary — no reader of free text can tell "rotate the production key" from "a worker
   rotated the production key", and this file contains both.
3. **The child is told where the job ends** — its instruction says the worktree is the
   boundary and to block the item rather than act on a running system. Not a control at all. A
   worker on the reported wave did exactly this unprompted; asking every time beats hoping.

What remains open, stated plainly: an agent planner can acknowledge its way past layer 2, a
`repo` default is only as good as the person who reviewed the item, and layer 3 persuades
rather than prevents. Scope waves to PRDs you control.

### Seeing a wave before you run it

```bash
gbfleet until --repo . --server <url> --project <id> --adapter <vendor> --prd <id> --dry-run
```

Prints what would be delegated — the free clusters, the held ones with who holds them and when
they free, and whether `--max-workers` is capping the list — then exits. It takes no lock,
mints no seat and cuts no worktree, so it can be asked while a wave is already running.

It calls the same `collision_clusters` the loop calls and applies the same split, rather than
modelling the wave separately: a dry run that models it can reassure you about a plan the loop
does not have.

### When a wave ends because of the vendor, not the fleet

A child that exits before registering reads as a broken adapter, and that is right for almost
every case. It was wrong for one whole class. A measured wave ended
`{"ok": false, "reason": "cap", "spawned": 6, "minted": 0}` after three children died in under
a second each, reported as `adapter 'claude': child exited 1 before registering. stderr tail:`
— followed by nothing, because stderr was empty. The cause was 67 bytes in `stdout.log`:

```
You've hit your session limit · resets 12:20pm (America/New_York)
```

So the wave blamed the adapter, sent the operator to the vendor CLI, and ended on `cap`, which
means *you hit your own `--max-children`* and invites raising it.

`until` now ends with `reason: "vendor_limit"` and quotes what the vendor said, including the
reset time, and stops rather than spending the rest of its children on a wall it has already
hit (GRPH-829). Both streams are read, and both are printed when it really is a crash — a tail
of the wrong stream is indistinguishable from a child that said nothing at all. Only `claude`
carries a measured string today; the base adapter matches nothing on purpose, because a
matcher that fired on the word "limit" would relabel real crashes as billing problems.

### Work recovered from a killed wave

A wave opens by adopting what the last one stranded, and a tree with real work in it is
committed as `WIP: salvaged by gbfleet`. That commit used to stay **local**: one measured
takeover recovered 614 insertions across exactly one item's touchpoints, and the item was
re-delegated minutes later, branched from `main`, and rebuilt every line. The work was
recovered and lost in the same move.

Salvaged work now gets the same two steps a finished child's work gets — pushed, then a draft
PR naming the item it belongs to, with the receipt written on the item (GRPH-830). The item id
comes from the dead child's own record, so a record written by an older supervisor still
publishes the branch and simply has nobody to hand the receipt to. A tree whose only
uncommitted file was the seat is still not published: `ONLY_CREDENTIAL` is not `SALVAGED`, and
a pushed empty branch per dead child would make every crash look like work.

### Why a wave is not spawning

`--max-workers 3` yielding one running child is a symptom anyone can see. Why, used not to be
readable anywhere — and on the reported wave that produced a confident wrong answer rather than
a slow one: with the repository open and the touchpoints in hand, the operator concluded an
item was being wrongly held and inferred directory-level clustering from the symptom. A later
spawn into a genuinely disjoint cluster disproved it.

`--dry-run` now carries two things it did not (GRPH-833):

- **why each held cluster is held** — which of its areas the reservation covers, whose it is,
  and under which rule (`exact`, `glob`, `directory`, `prefix`). `directory` is the broad one:
  every pair of files in one directory relates, so a directory of five files collapses five
  items into one cluster. That is a defensible clustering heuristic and a costly reservation
  rule, and it was impossible to tell which you were looking at.
- **the reservation table itself.** This is the one that was missing entirely, because a
  cluster is only in the partition while its items are claimable — the moment an item is
  claimed its cluster leaves the divvy, taking its still-blocking reservation off every read.
  A wave with no free clusters and no held ones had nothing to show for itself at all.

Each row says whether the hold is actually **blocking**. A reservation held by an agent the
roster calls offline still exists and is already ignored (GRPH-808); seeing the row without
that fact sends you looking for a collision that is not happening. `offline` and `retired` are
kept apart on purpose: an offline holder's lease will lapse, a retired seat can never register
again, and "wait" and "stop waiting" are different instructions.
### A drain that outlives your terminal

`gbfleet until` runs for as long as the process that started it, and no longer. That is not a
detail — it is the root of the memory kill above: the wave was a background task a harness
owned, so the harness stopped it. `gbfleet service` hands it to the machine's own supervisor
instead (GRPH-844):

```bash
gbfleet service install -- --repo /srv/graphban --server http://box:8080 \
    --project graphban --adapter claude --max-workers 2
gbfleet service status
gbfleet service uninstall
```

A LaunchAgent on macOS, a `systemd --user` unit on Linux. **User domain only** — a root
installer is a different program with different failure modes, and even the server's has never
been walked privileged. Everything after the subcommand goes to `until` unchanged, so
`gbfleet until --help` stays the authority on its own flags, and `--dry-run` prints the unit
without writing it.

Five things about it are worth knowing before you run it:

- **The PATH is the thing most likely to be wrong.** A supervisor's job gets a minimal PATH,
  and every vendor CLI the fleet exists to run lives somewhere that PATH does not contain. So
  the installing shell's PATH is captured into the unit, `status` prints it back, and `doctor`
  FAILs a service whose PATH is empty. A drain that starts, is reported running, and resolves
  no adapter is the failure this design most easily produces.
- **The key is never in the unit.** A unit file is world-readable. `until` takes its
  credential only from `$GBFLEET_API_KEY`, so `install` reads it from your shell and writes it
  to an owner-only file the unit references — `EnvironmentFile=` on systemd, a sourced `sh -c`
  on launchd, which has no equivalent. Two guards refuse a unit that would carry one anyway,
  and `uninstall` removes the key file with the unit.
- **`--every` is a real cost, not a formality.** `until` EXITS when there is no ready work,
  which is success — so the restart delay *is* the polling interval. Each cycle registers one
  planner agent (`register_agent` always creates a row; it never reuses one by label), so the
  default is 300s rather than seconds.
- **Not running is usually correct.** For most of every cycle a healthy drain is stopped, and
  the last exit code is what separates that from a dead one. `status` says `idle between runs,
  last exit 0`; `doctor` calls it PASS. Only a non-zero exit is a fault.
- **It holds the repo lock for the whole clone.** The lock is per git *common dir*, so while a
  drain runs, interactive `gbfleet up` on that checkout is refused — and a worktree is the same
  clone. Two drains want two clones. The install prints which path it will hold.

On a headless Linux box, `systemd --user` services stop when your last session ends, so a drain
installed over ssh dies at logout. `install` warns and `status` prints `linger:` every time;
`loginctl enable-linger <user>` is the whole fix.

### When the machine is the limit

A wave stopped mid-run with *"Background command was stopped because the system is running
low on memory"*, the supervisor gone and its children with it. Nothing in the fleet had any
idea: `up` could count seats, workers, children and wall-clock, and could not count the one
resource that actually ran out. The wave summary said `3 seat(s) never redeemed`, which reads
exactly like a cap, a crash or a broken adapter.

So the spawn loop asks the kernel before each child (GRPH-842):

```bash
gbfleet doctor
  [PASS   ] memory headroom — 8.8 GB available, 2.0 GB reserved, 0.7 GB per child: room for 9
```

```json
"gated": ["3.1 GB available, 2.0 GB reserved: no room for another 0.7 GB child"],
"headroom_bytes": 3328599654
```

**Both keys, always** — an empty `gated` means "nothing was refused" only when
`headroom_bytes` is a number. `null` means the host could not be asked and the gate never
bound, which is otherwise indistinguishable from a roomy machine.

Three things about it are deliberate:

- **It never blocks the first child.** A wave that spawns nothing produces nothing, and a bad
  reading would then cost the operator everything rather than one slot. The gate binds from
  the second child on, which is where the kill happened.
- **An unmeasurable host does not bind it.** `hostos.available_memory` answers `None` where
  the platform has no cheap way to ask, and `doctor` reports UNKNOWN. Grounding the fleet on
  a question nobody could answer is the opposite failure and just as expensive.
- **It divides a live reading; it does not model the machine.** The per-child figure is only
  a divisor. Measured on the 24 GB box where this happened: eleven live Claude Code processes
  came to 2.5 GB resident, the largest reading 671 MB peak `phys_footprint`, while Chrome, Arc
  and Cursor held 7.3 GB between them and the box read `207M unused` with no fleet running at
  all. Three children was about 2 GB. **The wave was the straw and not the load**, so a gate
  that guessed at fleet usage would have been gating the wrong thing.

A child spawned seconds ago has not reached its footprint yet, so each spawn is charged
against the reading taken when the loop began and the verdict uses whichever of the two is
worse. The live reading catches up within seconds and takes over.

### What a wave spent, and stopping it

The terminal JSON now carries a `spend` block, and every wave has one:

```json
"spend": {"tokens_in": 412000, "tokens_out": 38000, "tokens": 450000,
          "reported": 2, "unreported": 4, "by_child": [...]}
```

**`reported` and `unreported` are the point**, not decoration. `450000 tokens` reads as the
wave's cost, and it is not the wave's cost if four of six children were never counted — only
vendors that print a result record contribute, and today that is `gbagent` alone. A child that
said nothing contributes nothing, which is never the same as zero.

```bash
gbfleet until … --budget 2000000        # tokens, not currency
```

The wave ends `reason: "budget"` once the children that reported have spent that much. Running
children are left to their own ends: killing one spends the tokens and throws away the work,
which is the only outcome worse than going over.

**In tokens because that is what can be counted.** PRD-41 §7 already defines cost for this
system as tokens-to-sign-off, and a currency figure would need a per-vendor, per-model price
table that goes stale in silence — a stale price is worse than none, because somebody acts on
it.

**`--budget` is refused when the adapter reports nothing** (GRPH-834). A cap over `claude`
today is not a loose cap: nothing would ever be counted against it, so it could never fire, and
the wave would look bounded while being unbounded. The refusal names the adapters that do
report, before the lock and before any worktree.

### Work whose dependency has not landed

`done` in the ledger means *attested*, not *merged*. An attestation binds to a commit and
nothing claims that commit went anywhere — so an item can be finished while its work exists
only on a feature branch. Children branch from the remote default, so a dependent item would
be built without it, and `blocked_by` cannot warn you: it lists *unfinished* dependencies, and
this one is finished.

`until` therefore **holds** an item whose finished dependency's commit is provably not an
ancestor of the base, names the dependency and the commit, and moves to the next cluster
(GRPH-798). The wave continues on work that is buildable; the remedy is a merge.

A dependency whose commit this clone has never seen is reported and **not** acted on — that is
an unknown, not an absence, and refusing on it would stop every wave on a fresh clone. The
base is fetched once per wave first, or a stale remote-tracking ref would measure everything
as already merged.

This is a supervisor check and cannot be a server one: the server holds an item id and a
commit and has no repository to resolve them against. A human calling `delegate` by hand is
not protected by it.

Or hand the local surface to a planner over stdio:

```bash
GBFLEET_API_KEY=... gbfleet mcp --server https://cloud.agentldgr.dev
```

`spawn` starts **one** child and takes no count. The planner decides how many to run —
it holds both servers, so it can read `collision_clusters` and `get_backlog` itself, mint
that many seats, and call `spawn` once each. The supervisor executes.

**Delegate to a seat (PRD-36).** A parent that wants one item built on a cheaper model
does it in two calls and keeps working: `delegate(id, lane, tier, seat=true)` on the
Graphban server mints a worker seat *bound* to the item and returns its code; then
`spawn(enrolment_code=<code>, tier="cheap", item=<id>)` here. Registering on a bound seat
claims the item server-side, so the child holds it from its first call and never touches
`claim_cluster`; the spawn reply echoes the roster's `assigned` block (`claimed`, or
`taken` with who holds it). `tier` resolves through a table the operator names at launch —
`gbfleet mcp --tier cheap=gbagent:qwen3.6:35b-a3b-coding-mtp-det --tier
frontier=claude:opus` — fixed for the life of the process, and an explicit `adapter`
overrides it. `gbfleet until` takes the same `--tier` table plus `--request cheap|frontier`
for what its own delegations ask, and mints bound seats for the seeds it delegates, so the
divvy no longer decides what its children claim. The outcome comes back through the ledger
— the item moving on the board — never as a reply in the parent's context.

**The preference matrix (PRD-37).** A tier with no `--tier` flag resolves through
`src/gbfleet/matrix.toml`, a committed table of harness × model × lane × tier rows
with a status (`verified`, `unverified`, `failed`, `unregistered`) and the item that proved
it — facts only, reviewed like code. Resolution runs in a fixed order: the rows for the
tier → the project's policy (`local_only`, `allowed_harnesses`) → the user's profile (an ordered allowlist of harnesses, weights
over `cost`/`quality`/`latency`/`locality`, excludes) → `failed` rows out → what this
machine has installed → score → ties (verified first, then the user's own order, then the
row's `order`). Profile and policy come from the server: `gbfleet mcp` and `gbfleet until`
read them off `fleet_status` **once at launch** (a change is read at the next launch, PRD-36
D16) — the profile is the API key owner's, with a per-project override, edited in the Fleet
view under the Wave tab; the policy is the project's. A key whose owner has no profile, or a
server that cannot be reached at launch, resolves on matrix order and policy alone and the
explanation says `profile: none`. Cross-vendor review is the server's preference at `claim_review`,
not a matrix rule: `reviewer_cross_vendor` left with the `role` axis (PRD-39 S5). Every spawned child is told, in the same sentence
as its enrolment code, to register with `capabilities={vendor, model?, tier}` for what the
supervisor actually launched (GRPH-732), so the ledger can attribute its outcome; only a NAMED
model is declared, since a vendor default is unknowable from here. Measured `quality` and `latency`
also ride on `fleet_status` (per declared vendor × model × lane × tier, with `n`); the resolver
reads the cell for the lane being resolved, never a pooled one, and counts an axis only past
`n ≥ 5`. The spawn reply carries `resolution`: `source` (`flag`
or `matrix`), how many rows survived each step, what each step dropped and why, the winner
with its per-axis numbers, and the runner-up. An empty resolution is a tool error naming
the step that emptied it — there is no silent default. Measured axes need `n ≥ 5` before
they count and say `unmeasured` until then. `--matrix PATH` on `mcp`, `until` and `doctor`
swaps the file; `gbfleet doctor` prints every row against this machine and what each
role/tier would resolve to. The file is TOML, not the YAML the PRD first wrote, because
gbfleet is httpx-thin by requirement and `tomllib` is standard library.

**Name the project.** A credential that spans several projects resolves a call that names
none to its *default* project, and that is not where the seats were minted: the child
registers on the seat's project (the server takes it from the seat), but the supervisor's
roster read and the child's own reads land elsewhere — the child never appears on the
roster the supervisor polls and reads a backlog that is not its own. `gbfleet mcp`,
`gbfleet until` and `gbfleet doctor` take `--project <id>`, named on every call; `doctor`
fails a multi-project key that gives none. The child learns its project from the
registration reply and names it afterwards (GRPH-718, GRPH-719).

**What a wave reports about itself.** Beyond spawn and reap, `up` and `until` print three
findings the signing agent otherwise has no way to see, all measured from the worktrees the
supervisor already owns and none of them acted on:

- `COLLIDED <path>: changed on <branch>, <branch>` — two workers changed the same file. The
  failure the partition exists to prevent, observed rather than predicted: exact paths, no
  coverage rule, and true whether the touchpoints were wrong or the divvy was.
- `UNDECLARED <branch>: changed N file(s) no touchpoint covers` — the partition's INPUT was
  wrong. Compared against the declaration as it stood when work was handed out, which is the
  snapshot the divvy used. An item that declared nothing is not drift; its areas were
  predicted, which the board already marks.
- `BEHIND <branch>: cut from a base N commit(s) behind origin/main` — how much landed on the
  trunk while the child worked. Two agents can each be green on their own base and conflict
  on merge, and nothing else in the system can see it: the server has no git, the signing agent
  gets a branch with no indication of what its diff is against, and the child was cut from
  HEAD at spawn and never looked again. The trunk is fetched once per wave before measuring —
  a remote-tracking ref is only as fresh as its last fetch, and a check that skipped it would
  report every branch as current. `BEHIND unmeasured: <reason>` when it could not be asked,
  because that is not the same as nothing having moved.

Vendors and what each of them needs: [`docs/fleet-adapters.md`](https://github.com/asc-me/graphban/blob/main/docs/fleet-adapters.md).

## Development

```bash
cd fleet
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The suite fails loudly if the package is not installed, rather than testing an
uninstalled fallback and passing.

## Verified on the deployed instance

On 2026-09-05 a session delegated this item to a bound seat, and gbfleet resolved
the cheap tier through the preference matrix with no --tier flag.
