# Graphban — agent guide

Graphban is an agent-native dev tool: linear tracker + pgvector agent memory +
request triage + a code-structure graph, all operable by coding agents through
56 MCP tools (`POST /api/mcp`, JSON-RPC) that share one service layer with the
REST API and web UI. Local-Docker-first; stays fully offline by default (stub
embeddings/chat — real providers are opt-in env config).

This file is a map, not a manual. Deeper truth lives in [`docs/`](docs/README.md);
read the route for your task class, not the whole corpus.

## Operating loop

Every change, regardless of task class:

```bash
# Backend (from backend/; venv via `uv venv --python 3.12 .venv && uv pip install -e ".[dev]"`)
./.venv/bin/python -m pytest -q          # SQLite, ~45s. pytest is NOT on the host PATH.

# Backend against real Postgres+pgvector (what production runs — CI runs both):
docker run -d --name gb-pg -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=graphban_test -p 5544:5432 pgvector/pgvector:pg16
DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5544/graphban_test" \
  ./.venv/bin/python -m pytest -q        # also proves the Alembic chain from empty

# Frontend (from web/):
pnpm test && pnpm typecheck              # build with `pnpm build`

# Fleet supervisor (from fleet/) — a SEPARATE distribution with its own venv (PRD-22 D-e).
# Only needed when touching fleet/ or backend dependencies, which its thin-install guard reads.
uv venv --python 3.12 .venv && uv pip install -e ".[dev]"
./.venv/bin/python -m pytest -q          # refuses to run against an uninstalled tree

# Full stack:
docker compose up --build                # web :8080, api :8000; starts EMPTY by design
```

CI (`.github/workflows/ci.yml`) runs each of these on every PR, gated by
`dorny/paths-filter` so a web-only PR does not pay for two backend suites. A change is not done
until both database engines pass — SQLite and Postgres have separate vector-search
implementations (`services/memory.py`, `services/code_graph.py`) and only the
Postgres run executes the real `<=>` SQL and migrations.

**Then run it against real data before calling it done.** Not a bonus pass — the
highest-yield check available, and the one a green suite cannot substitute for. On
2026-08-08/09 it found four defects in a row that every test missed: a completeness
pass reporting `## Decisions from grilling` as a missing feature, a close report
telling a PM that "Problem" and "Goals" were never delivered, a classifications view
returning an empty list where the truth was "ten items nobody looked at", and a
separation-of-duties check reporting a clean pass because nothing recorded who did
the work. Each looked correct in tests and was obviously wrong in the first real
output. Deploy, then read what it actually says about a real PRD.

## Invariants (violating these is the review comment you'll get)

- **One service layer.** MCP tools, REST routers, and anything new call the same
  functions in `backend/app/services/`. Never duplicate domain logic in a router
  or tool handler.
- **Schema is owned by Alembic** on Postgres (`backend/alembic/versions/`,
  currently 0001–0098). SQLite/tests use `create_all`. Never edit an applied
  migration; add a new one.
- **AI providers only via `backend/app/providers/`** (`Embedder`/`ChatModel`/
  `Extractor` protocols, selected by `EMBED_PROVIDER`/`CHAT_PROVIDER`). Offline
  stub is the default; cloud deps stay lazy imports behind the `cloud` extra.
- **Frontend data access only via `web/src/lib/api.ts` + `queries.ts`**
  (TanStack Query). Query keys include the active project id.
- **Enums live in services** (`services/items.py:STATUSES`, `requests.py`,
  `links.py`, `code_graph.py`) — reference them; don't inline copies.

## Design defaults (weaker than invariants; argue with them if you have a reason)

- **Simplest thing that fully meets the requirement.** No speculative
  abstraction, configuration, or indirection. Config is the expensive one: 49
  settings already exist, and each new one is a permanent branch in every
  deployment's behaviour. PRD-14 proposed a per-project `profile` field and it
  was cut for exactly this — the need was imagined, and removing it made the
  design better, not smaller.
- **Grow in layers; never trade a working product for unfinished complexity.**
  Decompose so the first item is the smallest thing that works end to end and
  each later one lands on something that already runs. In practice: the root
  item ships first, the acceptance walk ships last, and every PR in between
  leaves `main` deployable.
- **Reach for what's already here.** Use the dependencies the project already
  has before adding one or writing your own — and check a library's docs and
  types before concluding it can't do the thing. Reimplementing is allowed, but
  the reason goes in a comment (see `_validate_args` in `mcp_server.py`, which
  says why it hand-rolls schema checking).
- **An absence must not read as a clean result.** When you report nothing — an empty
  list, a `false`, a zero, a null — ask whether it can also mean "nobody looked". If
  it can, it needs a third answer, because the quiet reading is always the
  reassuring one. `baseline_drift` returns `governed: False` rather than 0 for a PRD
  with no baseline; `completeness` separates `absent` from `undelivered`;
  `evidence_rollup` reports `unknown` for an item that claimed no touchpoints;
  `separation` distinguishes `independent` from `unverifiable`. That distinction was
  named once and then applied correctly four times unprompted — and every place it
  was NOT named grew the same defect independently, five times in one PRD. Naming it
  is what makes it travel; judgement alone did not.
- **Break it on purpose before you believe the tests.** For each load-bearing
  claim, revert that one behaviour and re-run: if the suite still passes, the
  test asserts something weaker than it reads. Then restore and note in the PR
  what failed and how many. This is cheap and its hit rate here has been high —
  it has caught a test excluding by filename where the guard needed a path, an
  assertion pinned to a value `create_prd` had already snapshotted, a grader test
  that passed only because the stub and the configured model happened to agree,
  and a set of hold tests that all claimed an item first, so the very gate the
  feature existed to remove passed every one of them. Two of those were tests
  reproducing the exact defect they were written to prevent, which no amount of
  re-reading them would have found. Green on both engines is necessary and is not
  evidence; a judgement surface especially can pass a full suite and still be
  wrong on contact with real data, so run it against a live PRD too.

- **Sabotage the CALL, not only the callee.** The same hole survived that
  instruction three tickets running: a pure function with thorough unit tests, and
  the surface that consumes it with none. `section_drift` could return
  `section_gone` and `coverage` iterated the PRD's own headings, so an item whose
  section was renamed away was unreachable (GRPH-360). `owns()` had six tests on
  what may be dropped and none on the call, so deleting `if not owns(...)` from the
  teardown broke nothing (GRPH-534). `classify_section` honoured `<!-- framing -->`
  while `coverage` could pass an empty body and drop the field from its payload,
  with 104 tests green (GRPH-247). Every one was caught by mutating the boundary,
  never the function. So when the thing you built feeds a report or a payload:
  make the caller pass an empty argument, drop the field, skip the branch that
  consults it. If the suite stays green you have shipped a correct function nobody
  calls — and here that means a clean report, which is the absence rule above
  wearing different clothes.

**Compatibility is NOT one of these defaults — the opposite rule applies.** Two
instances are deployed, API keys live in agents' configs, and the MCP tool names
are cached by clients. So: *an identifier that existing data is keyed by is
identity, not branding* (PRD-13). Removing an obsolete internal path is good
housekeeping; removing one something external is keyed by is data loss wearing a
tidy-up costume. `test_wire_name_compat.py` and `test_infra_identity.py` exist to
stop that, and a failure there is never a naming bug.

## Ledger loop

This repo tracks its own work in Graphban, and the MCP tools are exposed to every agent —
but exposing a tool does not say *when* to reach for it. That gap is why items sit in
`in_progress` with no evidence and why two agents pick up the same work.

1. **Claim before you start.** `claim_next` takes one ready item and moves it to
   `in_progress`; two agents never get the same one. It reserves no files, so in a fleet
   prefer `claim_cluster`. Read the item with `get_item_details` before writing code — the
   description usually names the trap.
2. **Heartbeat while you work.** `heartbeat` extends the lease so the item is not reclaimed
   mid-change. Without it a long task looks abandoned and someone else starts it.
3. **Record what you did, on the item.** `update_item(status=…, evidence=[…])` — evidence
   APPENDS, so add receipts as you get them rather than saving one summary. A status change
   with no receipt is indistinguishable from a placeholder move, and those have shipped here.
4. **Release what you did not finish.** `release_item` puts it back rather than leaving a
   claim nobody is working. Prefer `blocked` with a `blocker` over `backlog` when the reason
   will still be true tomorrow — `claim_next` reads backlog, so a known-unbuildable item
   parked there is handed straight back out.

Reach for the rest by question, not by habit: `get_backlog` / `search_items` for what is
open, `search_memory` and `related_work` for what was already learned or tried,
`get_code_map` / `code_neighbors` / `search_code` for structure, `describe_code` to write
structure back after you change it, and the PRD tools when the intent is unclear rather
than guessing at it.

## Task classes

### Add or change an MCP tool
1. Tool entry in `backend/app/mcp_server.py` `TOOLS` (description states purpose,
   invariants, and return shape — match `claim_next`/`describe_code` style) +
   handler branch in `_call_tool` calling a service function.
2. **Every tool needs an `outputSchema`** (asserted by `test_api.py`).
3. No count assertions to update — `LIVE_TOOL_COUNT` is `len(TOOLS)`, so
   `test_api.py` and `test_phase4.py` follow automatically. The prose counts in
   `docs/mcp.md`, `docs/README.md`, `docs/ARCHITECTURE.md`, `docs/product-overview.md`
   and this file are NOT derived, and nothing fails when they drift — update them.
4. Update the tool table in `docs/mcp.md`.
5. MCP round-trip test: POST a JSON-RPC `tools/call` envelope with an `X-API-Key`
   (see `test_api.py` for the pattern).

### Add a schema change (Postgres)
1. Edit models in `backend/app/models/__init__.py`.
2. `cd backend && ./.venv/bin/alembic revision --autogenerate -m "..."` — then
   **review the generated file**; the custom `EmbeddingType`/pgvector columns and
   raw-SQL indexes need hand-checking (autogen gets them wrong).
3. Verify the chain from empty: run the Postgres pytest command above (lifespan
   migrates on startup).
4. Vector indexes are **HNSW**, not ivfflat (migration 0016) — ivfflat built on
   empty tables silently loses recall.

### Add a view / frontend feature
Route in `web/src/App.tsx`, feature dir under `web/src/features/`, API methods in
`lib/api.ts` + hooks in `lib/queries.ts` (key on project id). Tests rendering a
view that touches `useProjectCtx` must wrap in `<ProjectProvider>` **inside a
router** and mock `api.projects`. Docs overlay: register the route in
`features/docs/content.ts`.

**There is no ambient project** (PRD-21 D1.1). Every project-scoped write takes the
id as its first argument — `createItem(projectId, body)` — and the active project is
derived from the URL, not from a module variable or an effect. Build org-plane paths
with the helpers in `lib/routes.ts`; a literal `"/org"` anywhere outside that file
fails `hierarchy.test.tsx`, because the base becomes `""` when an org is served from
its own host.

### Work on providers / embeddings
`docs/ai-providers.md`. Changing `EMBED_DIM` requires DB reprovision AND note
that migrations 0001/0013 pin 384 in the column type — see AL-46.

## Routes into docs/

| Task | Read |
| --- | --- |
| Any first contact | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/data-model.md`](docs/data-model.md) |
| MCP tools | [`docs/mcp.md`](docs/mcp.md) |
| REST surface | [`docs/api-reference.md`](docs/api-reference.md) |
| Dev workflows | [`docs/development.md`](docs/development.md) |
| Providers/AI | [`docs/ai-providers.md`](docs/ai-providers.md) |
| Config/env | [`docs/configuration.md`](docs/configuration.md) |
| Native install (launchd / systemd) | [`docs/native-install.md`](docs/native-install.md) |
| Fleet supervisor (`fleet/`) | [`fleet/README.md`](fleet/README.md), [`docs/prd-22-fleet-supervisor.md`](docs/prd-22-fleet-supervisor.md) |
| Vendor CLIs the fleet can run | [`docs/fleet-adapters.md`](docs/fleet-adapters.md) |
| Does the fleet actually work? | [`docs/fleet-supervisor-walk.md`](docs/fleet-supervisor-walk.md) |
| Where an agent's tokens go | [`docs/token-census.md`](docs/token-census.md), `scripts/token_census.py` |

## PRDs live in the ledger

**The ledger is the source of truth for a PRD. A `docs/prd-*.md` copy is optional.**

Most PRDs have no repo document, and plenty of those are past `draft`. PRD-24 is approved with
its slices built and has none; PRD-16, PRD-18 and PRD-13 are finished specs living only in the
ledger, and PRD-13's project-tag invariants are what GRPH-319, GRPH-457 and GRPH-459 all turn
on.

**No count appears here, on purpose (GRPH-558).** The previous version of this paragraph gave
one — and every figure in it was wrong within two days, because PRDs are created faster than a
hand-typed census is revisited. A number in this file has nothing keeping it true, and this
repository has already carried the MCP tool count as three different values in three places at
once. Generating it is not available either: the tests that keep `docs/api-reference.md` and
the tool count honest work because routes and the manifest are readable from the app offline,
and nothing in CI can reach the ledger. So the rule is stated without arithmetic, which costs
nothing — it does not depend on how many PRDs there are.

An earlier expectation that a PRD past `draft` should exist in both was never written down and
never held. Stating it now would make a rule nobody follows, and enforcing it would fail on
every ledger-only spec in the one direction a test cannot fix — which trains people to ignore
the test. So it is dropped, deliberately, and this paragraph is the record of that (GRPH-465).

There is no rule about WHEN a repo copy appears, and no honest one available: the five that
have docs look like recent implementation work until you notice PRD-24 is newer than all of
them and has none. Write one when a spec is worth reviewing in a diff.

What IS enforced, by `backend/tests/test_prd_sync.py`: **a repo PRD must exist in the ledger
and agree with it.** That is the direction where drift is silent and expensive — PRD-17 read
`draft` in the repo for eleven days while the ledger had it `approved`, through the whole
build and the acceptance walk.

Two consequences worth knowing before you build on either:

- `docs/prd-index.json` indexes only PRDs that have a repo doc, so it is **a fraction of them
  and not the list of PRDs**. Reading it as one produces a confident undercount (GRPH-486).
  The file says so itself, in a `scope` block that deliberately states no ledger total — the
  generator cannot obtain one, and a number it cannot check is the confident wrong answer that
  note exists to prevent.
- `prd_coverage` and `prd_acceptance` answer from the ledger, which is complete. Prefer them.

## Deploy

Full runbook: **[`docs/deploy.md`](docs/deploy.md)** — the proven `rsync` +
`docker compose` self-host deploy, with verification, recovery, and rollback.
The essentials: stamp `GIT_SHA=$(git rev-parse --short HEAD)` and pass it through
to `docker compose up -d --build`; **always `rsync --exclude .env --exclude sync`**
(the server keeps its own `.env` with remapped ports, and `sync/` is a root-owned
mount); verify the exact revision went live via `/health` (`git_sha` + `db`).

## Tracker

This repo tracks its own work in Graphban (project `graphban`). Current
priorities and the 2026-07 harness-review findings are items AL-40…AL-57 —
`get_backlog` / the Tracker view are the source of truth.
