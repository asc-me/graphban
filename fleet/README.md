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

Because seats are bound (PRD-36), scoping the delegation scopes the whole wave: a child claims
its own item and cannot call `claim_cluster` to reach around it.

**`--prd` refuses a server that does not support it.** An older Graphban does not reject an
unrecognised `prd_id` — it drops the property and answers the unfiltered question, so the flag
would drain the project *while reporting the wave as scoped*. Before the first spawn, `until`
asks for a PRD that cannot exist: a server that filters returns nothing, one that ignores the
argument returns the project, and the second refuses the run naming what it would otherwise
have done (GRPH-800). Needs the server-side filter; upgrade if you see that refusal.

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
