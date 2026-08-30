# Graphban documentation

**Agent memory. Linear execution.** An agent-native dev tool: a skinny linear tracker +
persistent agent memory (pgvector) + request triage + a PRD editor, with native MCP tools
so agents read/write project context through the *same code path* as the web app. Runs
fully offline with `docker compose up`.

## Start here

- **[Product overview](product-overview.md)** — vision, goals, target users, philosophy, licensing
- **[Design philosophy](design-philosophy.md)** — the harness-engineering principles the codebase is built to, and how they're enforced
- **[Getting started](getting-started.md)** — install, run, first login, a guided tour
- **[Architecture](ARCHITECTURE.md)** — how the system fits together and why

## Feature guides

| Guide | What it covers |
| --- | --- |
| [Tracker](tracker.md) | The linear item stream — states, drag-reorder, inline status, detail panel, filters |
| [Memory & agent chat](memory-and-chat.md) | Memory shards, semantic search, auto-extraction, import/export, streaming chat |
| [Requests & feedback](requests-and-feedback.md) | Triage queue, the public embeddable form, auto-duplicate detection, the Feedback Kit |
| [PRDs](prds.md) | List + markdown editor, live preview, version history + diff, AI commands, item links |
| [Links graph](links-graph.md) | Interactive typed-relationship graph |
| [Dashboard](dashboard.md) | KPIs, status distribution, request breakdown, recent activity |
| [Roadmap](roadmap.md) | Phased milestones + the shareable public roadmap |
| [Settings & profile](settings.md) | AI provider switch, integrations, project config, members, API keys, profile |

## Reference

| Doc | What it covers |
| --- | --- |
| [MCP tools](mcp.md) | The 56 MCP tools, JSON-RPC endpoint, API-key auth, error taxonomy, call metering |
| [Grok Build](grok-build.md) | Connect Grok Build (xAI's coding CLI) to Graphban's MCP + prime it on the loop |
| [Cursor](cursor.md) | Connect Cursor to Graphban's MCP (user + Cursor 3 Team scope) + the sub-agent fleet |
| [Swamp integration](swamp-integration.md) | **Design.** Running Graphban beside Swamp — gates that refuse, the attestation port, the licensing boundary |
| [AI providers](ai-providers.md) | The provider abstraction — stub / Ollama / Anthropic / OpenAI |
| [API reference](api-reference.md) | Every REST + public endpoint |
| [Data model](data-model.md) | Entities and relationships |
| [Configuration](configuration.md) | Environment variables |
| [Development](development.md) | Local dev, tests, migrations |
| [Deploy](deploy.md) | Self-host deploy runbook — release identity, verification, recovery, rollback |
| [Implementation plan](IMPLEMENTATION_PLAN.md) | The original phase plan, kept as a record — see the roadmap for current status |

## Conventions used in these docs

- **UI** paths are described from the left nav / top bar.
- **API** endpoints are all under `/api` (e.g. `POST /api/items`), JWT-authed unless noted
  **public** (unauthenticated) or **MCP** (API-key authed).
- The app starts empty — create an account and your first project in the UI. A demo dataset
  is available opt-in via `SEED_ON_START=true` (see [Getting started](getting-started.md)).
