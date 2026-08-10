# Graphban — agent guide

Graphban is an agent-native dev tool: linear tracker + pgvector agent memory +
request triage + a code-structure graph, all operable by coding agents through
36 MCP tools (`POST /api/mcp`, JSON-RPC) that share one service layer with the
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

# Full stack:
docker compose up --build                # web :8080, api :8000; starts EMPTY by design
```

CI (`.github/workflows/ci.yml`) runs all three on every PR. A change is not done
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
  currently 0001–0058). SQLite/tests use `create_all`. Never edit an applied
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

**Compatibility is NOT one of these defaults — the opposite rule applies.** Two
instances are deployed, API keys live in agents' configs, and the MCP tool names
are cached by clients. So: *an identifier that existing data is keyed by is
identity, not branding* (PRD-13). Removing an obsolete internal path is good
housekeeping; removing one something external is keyed by is data loss wearing a
tidy-up costume. `test_wire_name_compat.py` and `test_infra_identity.py` exist to
stop that, and a failure there is never a naming bug.

## Task classes

### Add or change an MCP tool
1. Tool entry in `backend/app/mcp_server.py` `TOOLS` (description states purpose,
   invariants, and return shape — match `claim_next`/`describe_code` style) +
   handler branch in `_call_tool` calling a service function.
2. **Every tool needs an `outputSchema`** (asserted by `test_api.py`).
3. Update the count assertions: `tests/test_api.py` (`len(names)`, `tool_count`
   ×2) and `tests/test_phase4.py` (`data["live"]`, `len(data["tools"])`).
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
view that touches `useProjectCtx` must wrap in `<ProjectProvider>` and mock
`api.projects`. Docs overlay: register the route in `features/docs/content.ts`.

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
