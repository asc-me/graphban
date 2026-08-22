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
[Apache-2.0](LICENSE), and the divergence is a decision (PRD-22 §8), not an oversight:

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

## Running it

One wave, deterministically — you mint the seats, it spawns and reaps:

```bash
GBFLEET_API_KEY=... gbfleet up \
    --server https://cloud.agentldgr.dev --seats-file seats.txt --adapter claude
```

Or hand the local surface to a planner over stdio:

```bash
GBFLEET_API_KEY=... gbfleet mcp --server https://cloud.agentldgr.dev
```

`spawn` starts **one** child and takes no count. The planner decides how many to run —
it holds both servers, so it can read `collision_clusters` and `get_backlog` itself, mint
that many seats, and call `spawn` once each. The supervisor executes.

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
