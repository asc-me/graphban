# Graphban — Implementation Plan (PRD → build)

Roadmap from the shipped **v0.1 core slice** to the full product (see
[Product overview](product-overview.md)). Each phase is an independently shippable
increment. Sequence is a recommendation —
phases 1–5 can be reordered; the **cross-cutting foundations** below should land first
or be woven into whichever phase needs them.

## Original plan, as written

**This table is the plan as it stood when it was written, kept as a record. It is not
current status** — it said Phase 6 "not started" for as long as the hosted plane was
live and deployed. For what has actually shipped, read [the roadmap](roadmap.md), which
computes `x / y shipped` from milestones instead of restating it by hand, and is why it
has not drifted.

| Item | State |
| --- | --- |
| **F1** LLM provider abstraction (stub/Ollama/Anthropic/OpenAI) | ✅ shipped |
| **F2** Alembic migrations (0001–0004) | ✅ shipped |
| **F3** SSE — chat streaming | ✅ shipped · per-project live-invalidation SSE ⏳ deferred |
| **F4** AuthZ hardening | ⏳ deferred (JWT + API keys + memberships in place) |
| **Phase 1** Real memory intelligence + all 11 MCP tools | ✅ shipped |
| **Phase 2** Public feedback + auto-duplicate detection + Feedback Kit | ✅ shipped |
| **Phase 3** PRD tracker + markdown editor | ✅ shipped |
| **Phase 4** Links graph + Dashboard + Roadmap + MCP Tools page | ✅ shipped |
| **Phase 5** Platform settings + integrations config + Settings/Profile | ✅ shipped (live GitHub webhook; outbound OAuth/sync deferred) |
| **Phase 6** Multi-tenant hosted + productionization | ⏳ not started (managed-service layer) |
| Odds & ends | ⏳ saved filters (R-35), first-class deps/blockers, outbound GitHub/Drive sync |

Every left-nav view is live and backed by real endpoints; 49 backend + 8 frontend tests
pass; the full migration chain runs clean on a fresh Postgres via `docker compose up`.
Details of each shipped increment are in the project memory and `docs/ARCHITECTURE.md`.

## Where we are (v0.1, shipped)

| PRD § | Done | Remaining |
| --- | --- | --- |
| 4.1 Linear tracker | linear stream, 6 states, tags/effort/dates, drag-reorder, inline status, quick filters, detail panel | first-class dependencies/blockers, saved filters |
| 4.2 Agent memory | shards (item/global), semantic search (**stub** embeddings), retrieval-grounded chat, add shard | real embeddings, auto-extraction, import/export, real LLM chat |
| 4.3 Requests | triage queue, types, votes, link-to-item | public embeddable form, auto-duplicate detection |
| 4.4 MCP | 5 live tools sharing the service layer, API-key auth | 6 more tools → all 11 live |
| 4.5 PRD editor | — | entire feature |
| 4.6 UI templates | tracker, memory sidebar, request/item components | Links graph, Dashboard, Roadmap, Feedback generator, a11y/responsive pass |
| 4.7 Integrations | — | GitHub, Google Drive, LLM hooks/Platform Settings |
| 4.8 Self-host/hosted | Docker Compose one-command | SQLite-first packaging, multi-tenant hosted + tiers |

Key existing seams to reuse: `backend/app/services/` (one code path for REST + MCP),
`backend/app/embeddings.py` (`Embedder` protocol — swap the stub), `backend/app/mcp_server.py`
(`TOOLS` list + `_call_tool` dispatch), `web/src/lib/{api,queries,types,meta}.ts`,
`web/src/components/ui/*` (shadcn-style primitives), the left-nav `SOON` items.

## Cross-cutting foundations (do first / early)

These unblock multiple phases; land them before the features that depend on them.

- **F1 · LLM provider abstraction.** Extend the `Embedder` seam into a full
  `providers/` package: `Embedder`, `ChatModel` (streaming), `Extractor`. Adapters for
  **Ollama** (local, `nomic-embed-text` + `llama3.1`) and **one cloud** (Claude via the
  API, cloud embeddings). Config: mode (local/cloud), base URL, model — surfaced by the
  Platform Settings view (Phase 5) and env. This is the linchpin for Phases 1, 2, 3.
  → Replaces the stub without touching call sites.
- **F2 · Alembic migrations.** v0.1 uses `create_all`. Introduce Alembic before any
  production schema change; generate the baseline from current models, gate future
  changes on migrations. `EMBED_DIM` becomes provider-driven (pad/re-embed on switch).
- **F3 · Real-time (SSE).** An `/api/events` SSE stream (per project) for live tracker/
  memory/request updates and token streaming for chat. Frontend: an `useEventStream`
  hook that invalidates the relevant TanStack Query keys.
- **F4 · AuthZ hardening.** Enforce membership roles on writes, refresh-token rotation,
  per-API-key scopes + rate limits (needed before public/hosted exposure).

## Phase 1 — Real memory intelligence  *(PRD 4.2, 4.4)*

Turn the memory system from "stub + retrieval echo" into the real thing, and finish the
MCP surface.

- **Embeddings for real** (needs F1): embed on write via the configured provider; **re-embed
  on edit** (fixes seeded request R-27, stale embeddings); backfill job for existing shards.
- **Auto-extraction**: when an item transitions to `done`, an `Extractor` distills
  decisions/learnings into shards attached to the item (respect `project.auto_extract`).
- **Real LLM chat**: replace the deterministic reply in `routers/agent.py` with a
  provider chat call, streamed over SSE (F3), grounded on top-k shards + project state;
  token-budget truncation (the AL-12 seeded item's own spec).
- **Memory import/export**: `GET/POST /api/memory/export|import` (JSON), UI in the sidebar.
- **MCP → all 11 live**: add `get_backlog`, `get_item_details`, `link_items` (needs the
  link model, Phase 4 — or ship a minimal typed-link table here), `suggest_next` (ranks
  from state + memory), `extract_lessons`, `generate_digest` (periodic cross-project
  digest). Register each in `mcp_server.TOOLS` + `_call_tool`; back each with a service fn.
- **Verify**: switch provider to Ollama and confirm ranking quality; complete an item →
  a lesson shard appears; stream a chat answer; call all 11 tools via MCP; the MCP Tools
  page (Phase 4) shows them live.

## Phase 2 — Requests & public feedback  *(PRD 4.3, 4.6)*

- **Public embeddable form** (seeded AL-19): unauthenticated `POST /api/public/requests`
  (rate-limited, project-scoped token), a standalone `/embed/feedback` route, and a small
  embeddable widget bundle + `<script>`/iframe snippet.
- **Auto-duplicate detection** (seeded AL-21): on submit, embed the request and surface
  similar existing items/requests above a similarity threshold before it enters triage;
  offer link-or-create.
- **Feedback Components generator** (design's "Feedback Components" view): themeable
  components (accent/mode/radius/font from the design's `fbCfg`), live preview, copyable
  embed code; capture options (screenshot/page/device metadata).
- **Saved filters** (seeded R-35) across Tracker + Requests.
- **Verify**: submit via the embedded form from a blank page → lands in triage with source
  metadata and a duplicate suggestion; generated snippet renders with the chosen theme.

## Phase 3 — PRD tracker & markdown editor  *(PRD 4.5)*

- **Data + API**: `prds` (title, status draft/review/approved, version, body markdown) +
  `prd_versions` history; link table PRD ↔ items and PRD ↔ shards.
- **PRD list view** (design "PRD List"): status + version columns, linked-item chips.
- **Markdown editor** (design "PRD Editor"): live preview, templates, version history
  sidebar with diff, status workflow.
- **AI commands** (needs F1): "Expand section", "Generate risks", etc. as `Extractor`/chat
  calls operating on the current doc; streamed.
- **Verify**: create a PRD from a template, run an AI command, cut a version, link it to
  AL-08; the version diff renders.

## Phase 4 — Links, Dashboard, Roadmap, MCP page  *(PRD 4.6, 4.4)*

- **Typed link model**: `links(a, b, type[dependency|code|semantic|tag], confidence, reason)`;
  service + `link_items` MCP tool; auto-suggest semantic links from embeddings.
- **Links graph** (design "Links"): interactive SVG canvas (nodes = items/requests, typed
  edges, filters, node/link detail) — port the design's `gnodes/glinks/buildGraph` logic
  to React over live data.
- **Dashboard** (design "Dashboard"): status distribution, velocity, recent activity,
  memory/MCP stats widgets.
- **Roadmap (lite)** (design "Roadmap"): milestone columns (MVP/Post-MVP/Later) with
  progress, sourced from items + a `milestones` table; copyable public roadmap link.
- **MCP Tools page** (design "MCP Tools"): per-tool live/planned, params, **call counts** —
  add lightweight per-key/per-tool usage metering in `mcp_server.py`.
- **Verify**: graph highlights a node's neighbors; dashboard numbers match the DB; roadmap
  reflects item statuses; MCP page shows real call counts.

## Phase 5 — Integrations & hooks  *(PRD 4.7)*

- **Platform Settings view** (design "Platform Settings"): LLM mode/provider/base-URL/model
  (drives F1), connection status, GitHub/Drive connect.
- **GitHub**: OAuth, issue/PR sync (make the item `pr` field real, two-way — seeded R-31),
  create issues from items, inbound webhooks → tracker/requests.
- **Google Drive**: OAuth, import/link docs, markdown sync into PRDs/memory.
- **Settings/Profile/Team** views (design "Settings"/"Profile"): project config, membership
  roles/access (enforced via F4), API-key management UI (partially exists).
- **Verify**: connect a GitHub repo, sync an issue → item, push a status → reflects on the
  PR; connect Drive, import a doc into a PRD.

## Phase 6 — Multi-tenant hosted & productionization  *(PRD 4.8 + tech)*

- **Multi-tenancy**: org/workspace isolation, row-level scoping, invitations.
- **Usage tiers**: free/pro/team quotas (projects, memory, MCP calls), billing hooks.
- **Productionization**: F2 migrations in CI, backups, observability/metrics, secrets, and
  a documented **SQLite-first** single-binary/compose profile as a first-class deploy mode
  (the store layer already branches; package + test it).
- **Verify**: two isolated orgs can't see each other's data; quota enforcement trips; a
  fresh SQLite deploy boots with one command.

## Sequencing & dependencies

```
F1 LLM providers ─┬─> Phase 1 (memory) ─┬─> Phase 4 (links/dashboard/MCP page)
F3 SSE ───────────┘                     │
F2 Alembic ──> (before prod schema change; needed by Phases 3,4,6)
                  └─> Phase 2 (feedback) ┘
F1 ──> Phase 3 (PRD editor)
F1 + F4 ──> Phase 5 (integrations / Platform Settings)
F4 ──> Phase 6 (hosted)
```

Recommended order: **F1 → F2 → F3** interleaved with **Phase 1**, then **2 → 3 → 4 → 5 → 6**.
Each phase ends runnable and demoable via `docker compose up` + `/verify`, and adds tests
(pytest for services/MCP, Vitest for views) in the pattern established in v0.1.

## Open decisions

1. **Cloud provider**: which one for the cloud adapter (Claude assumed for chat/extract;
   embedding model TBD)? Local Ollama is the default per the PRD.
2. **Phase 1 vs 4 for the link model**: `link_items`/`extract_lessons` want the typed-link
   table; either ship a minimal version in Phase 1 or defer those two tools to Phase 4.
3. **Real-time transport**: SSE (simpler, assumed) vs WebSocket (bidirectional) — SSE
   covers streaming + invalidation for this app.
4. **Priority**: is the next build **memory intelligence** (Phase 1) or **public feedback**
   (Phase 2)? Both are high-value; the recommendation leads with Phase 1.
