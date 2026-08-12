# Getting started

## Run it (Docker — one command)

```bash
cp .env.example .env      # optional; defaults work with zero external services
docker compose up --build
```

The stack is **Postgres (pgvector) + FastAPI + an nginx-served SPA**. On first boot the API
enables the `vector` extension and runs Alembic migrations. The database starts **empty**.

| Surface | URL |
| --- | --- |
| Web app | http://localhost:8080 |
| API | http://localhost:8000 (`/health`, OpenAPI at `/docs`) |
| Public feedback widget | http://localhost:8080/embed/feedback |
| Public roadmap | http://localhost:8080/embed/roadmap |

## First run — create your account

Open the web app and, on the sign-in screen, choose **Create an account**. Register with a
name, handle, email, and password, then **Create your first project** — a workspace for your
tracker, agent memory, requests, and PRDs. You can add more projects later from the switcher
at the top of the left nav.

Ports and credentials are configurable via `.env` — see [Configuration](configuration.md).

## Optional: load the demo dataset

To explore a fully-populated app instead of starting empty, set `SEED_ON_START=true` in `.env`
**before the first `docker compose up`** (seeding only runs on an empty database). It loads a
dataset mirroring the design prototype:

- 3 projects (Core Platform, Web App, Infra), 4 users with roles/memberships.
- 9 tracker items across all six states (with tags, effort, PR metadata).
- 5 requests, 5 memory shards, a typed link graph, a roadmap, PRDs with version history.
- Seeded MCP call counts and a platform config (offline stub provider; GitHub shown connected).

The seeded users share the password `graphban` (e.g. sign in as `alex@ascme-labs.com`) —
change it for any real deployment. To clear demo data later, set `SEED_ON_START=false` and
either truncate the tables or run `docker compose down -v` to reset the volume, then sign up
fresh.

## A guided tour

1. **Tracker** (landing view) — the linear item stream. Click a status dot to advance an
   item, drag a row to reorder, click a row to open its detail panel. → [Tracker](tracker.md)
2. **Agent sidebar** (right, toggle with **Agent**) — semantic **Memory** search and a
   streaming **Chat** grounded in project state + memory. → [Memory & chat](memory-and-chat.md)
3. **Requests** — the triage queue. The public form that feeds it lives at
   `/embed/feedback`. → [Requests & feedback](requests-and-feedback.md)
4. **PRDs** — open one to see the split markdown editor, live preview, version diff, and AI
   commands. → [PRDs](prds.md)
5. **Links / Dashboard / Roadmap / MCP Tools** — the analytics views.
6. **Settings** (bottom of the nav) — switch the AI provider, connect integrations, manage
   members and API keys. → [Settings & profile](settings.md)

## Talk to it as an agent (MCP)

1. In **Settings → API Keys**, create a key and copy it (shown once).
2. Call the MCP endpoint:

```bash
curl -s http://localhost:8000/api/mcp \
  -H "X-API-Key: gb_sk_..." -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"create_item","arguments":{"title":"From an agent","effort":2}}}'
```

The item appears immediately in the Tracker. → [MCP tools](mcp.md)

## Turn on a real LLM (optional)

By default all AI runs on an offline **stub**. To use a real model, open **Settings → AI
Providers** and pick **Local (Ollama)** or **Cloud (Claude)**, or set `CHAT_PROVIDER` in the
environment. → [AI providers](ai-providers.md)

## Develop locally (without Docker)

See [Development](development.md) for backend (`uvicorn` + `pytest`) and frontend
(`pnpm dev` + `vitest`) workflows.
