# Graphban — Architecture

A decoupled **Vite + React SPA ↔ FastAPI ↔ Postgres/pgvector**, orchestrated by Docker
Compose. Built local-first so the whole thing runs offline; cloud LLMs, embeddings, and
integrations are opt-in behind stable interfaces. This document explains how the pieces
fit and why.

## System overview

```
                         ┌───────────────────────────── browser ─────────────────────────────┐
                         │  SPA (React 19 + TS, Tailwind v4, TanStack Query)                   │
                         │  shell · tracker · memory/agent · requests · prds · links ·         │
                         │  dashboard · roadmap · mcp · feedback-kit · settings · profile      │
                         │  public: /embed/feedback  /embed/roadmap                            │
                         └───────────────┬───────────────────────────────┬────────────────────┘
                                         │ REST + SSE (JWT / refresh)     │ public (no auth, rate-limited)
                    ┌────────────────────▼────────────────────┐          │
   agents ── MCP ──▶│                FastAPI                   │◀─────────┘
  (X-API-Key)       │  routers → SERVICES (single code path) → SQLAlchemy models             │
                    │                     │                                                    │
                    │            providers (F1): Embedder · ChatModel · Extractor              │
                    │            stub (default) │ Ollama │ Anthropic │ OpenAI-embed             │
                    └───────────┬───────────────┴───────────────────────────┬─────────────────┘
                                │                                            │ (opt-in)
                    ┌───────────▼────────────┐                   ┌───────────▼───────────┐
                    │ Postgres + pgvector    │                   │ Ollama / Claude / etc. │
                    │ (Alembic-migrated)     │                   └────────────────────────┘
                    └────────────────────────┘
```

## The load-bearing idea: one service layer

`backend/app/services/` holds all business logic (items, memory, requests, prds, links,
duplicates, insights, dashboard, roadmap, mcp_stats, platform). **Both** the REST routers
**and** the MCP server call these functions — there is no duplicated logic and no drift.
An agent's `create_item` over MCP and a user's "New item" in the web app run the identical
code, which is why an MCP write appears instantly in the UI.

- **REST** (`routers/*.py`) — thin: parse/validate → call a service → serialize. JWT auth.
- **MCP** (`mcp_server.py`) — a JSON-RPC 2.0 HTTP endpoint (`POST /api/mcp`) exposing 11
  tools, authed by a scoped API key, dispatching to the same services. Each `tools/call`
  is metered (`mcp_tool_stats`) for the MCP Tools page.
- **Public** (`routers/public.py`) — unauthenticated, rate-limited intake for the embed
  widgets and the GitHub webhook.

## The provider abstraction (F1)

`backend/app/providers/` defines three protocols — `Embedder`, `ChatModel`, `Extractor` —
with a registry that selects an implementation from config:

| Provider | Chat / extraction | Embeddings | Deps |
| --- | --- | --- | --- |
| **stub** (default) | deterministic composed reply | hashed bag-of-tokens → L2-normalized vector | none |
| **ollama** | `/api/chat` (+ streaming) | `/api/embeddings` | httpx |
| **anthropic** | Claude Messages API | — (no embeddings endpoint) | `anthropic` (optional extra) |
| **openai** | — | `/v1/embeddings` | httpx |

The stub keeps the stack fully offline and makes tests deterministic. Everything that
touches an LLM — semantic search, auto-extraction on completion, agent chat, PRD AI
commands — goes through these interfaces, so a real provider drops in without touching
call sites. **Settings → AI Providers** switches the chat provider live (updates the
in-memory settings + resets the provider cache). Embeddings stay a deploy-time choice
because the pgvector column dimension is fixed at migration time.

## Semantic memory

Memory shards are embedded on write and searched by cosine similarity. On Postgres this
uses the pgvector `<=>` operator with an ivfflat index; a Python cosine fallback covers
the SQLite/no-extension path. A dialect-aware `EmbeddingType` stores a real `vector`
column on Postgres and JSON on SQLite. Editing a shard re-embeds it (fixes stale
embeddings); `POST /api/memory/backfill` re-embeds everything after a provider switch.

## Streaming (F3, chat)

`POST /api/agent/chat/stream` returns `text/event-stream`: a `shards` event, then `delta`
token events, then `done`. Each `ChatModel` has a native `stream()` (stub chunks; Ollama
reads ndjson; Anthropic uses `messages.stream`), with a non-streaming fallback. The SPA
reads it via `fetch` + `ReadableStream` (not `EventSource`, so it can send the JWT header)
and appends deltas into the agent bubble. Per-project live-invalidation SSE is a
follow-on; only chat streaming is wired today.

## Persistence & migrations (F2)

Postgres schema is owned by **Alembic** and runs on API startup; SQLite (tests, zero-infra
dev) uses `create_all`. Migrations to date:

```
0001 initial     users, projects, memberships, items, memory_shards (pgvector + ivfflat),
                 requests, links, api_keys
0002 prds        prds, prd_versions
0003 roadmap_mcp milestones, mcp_tool_stats
0004 platform    platform_config
```

## Auth

- **JWT** — `POST /auth/login` issues access + refresh tokens. The SPA keeps the access
  token in memory and the refresh token in localStorage; a 401 triggers one silent refresh
  + retry. Passwords are bcrypt-hashed.
- **API keys** — `gb_sk_…`, shown once, stored SHA-256-hashed, scoped. They authenticate
  the MCP endpoint (`X-API-Key` or `Authorization: Bearer gb_sk_…`).
- Users have **memberships** (owner/admin/member + per-project access), surfaced in
  Settings → Members and Profile.

## Frontend

Vite + React 19 + TypeScript. Tailwind v4 with the design tokens as CSS variables
(dark-only). Data via TanStack Query over a typed `lib/api.ts` (auth pipeline with refresh)
and a separate `lib/publicApi.ts` (unauthenticated). UI primitives are shadcn-style
(Radix + cva) themed to the design. A dependency-free markdown renderer (`lib/markdown.tsx`)
and LCS line-diff (`lib/diff.ts`) power the PRD editor; the Links graph uses a small
deterministic force-directed layout. Charts follow the dataviz method (status/type palettes
with labels, rounded ends, 2px gaps, ink-token text).

## Directory layout

```
backend/app/
  main.py            FastAPI app, lifespan (migrate/seed/apply-provider-config), routers
  config.py          pydantic-settings (DB, JWT, providers, CORS, EMBED_DIM …)
  db.py              engine, session, dialect-aware EmbeddingType host
  models/            SQLAlchemy models (one module)
  schemas/           Pydantic request/response models
  security/          jwt · apikey · passwords · deps (get_current_user / get_agent_key)
  providers/         base protocols + stub · ollama · openai · anthropic + registry
  services/          business logic shared by REST + MCP
  routers/           auth · projects · items · requests · memory · agent · apikeys ·
                     prds · analytics · platform · reports · public · admin ·
                     artifacts · assistant · fleet · learning · orgs · sync
  mcp_server.py      JSON-RPC MCP endpoint (55 tools) + metering
  seed.py            the design dataset
alembic/             migration chain (0001 → head)

web/src/
  lib/               api · publicApi · queries · types · meta · cn · markdown · diff
  components/ui/     shadcn-style primitives
  components/shell/  TopBar · ProjectBar · LeftNav · AgentSidebar · AppFrame
  features/          auth · tracker · requests · memory(sidebar) · prds · links ·
                     dashboard · roadmap · mcp · feedback · settings · profile
```

## Deliberate boundaries (local slice)

- **No live GitHub/Drive OAuth or outbound sync.** Connection config is real and
  persisted; the inbound GitHub webhook is fully implemented; outbound push requires a
  connected token and is out of scope offline (the API says so honestly).
- **Multi-tenancy is built and gated, not absent.** `services/orgs.py`, `services/quotas.py`
  and `routers/admin.py` are in the tree; the operator plane is hidden behind
  `settings.hosted_mode` plus a platform-admin allowlist, and `require_platform_admin`
  answers 404 rather than 403 so a tenant cannot see that it exists. A self-hosted
  instance runs single-tenant because the flag is off, not because the code is missing.
  See [PRD-21](prd-21-cloud-org-plane.md). Remaining Phase 6 work is productionization —
  observability, backups, SQLite-first packaging.
- **In-memory rate limiting** on public endpoints (per-process) — fine for local/single
  instance; a shared store is needed for multi-instance.
