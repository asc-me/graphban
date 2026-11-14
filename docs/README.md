# Graphban documentation

**Agent memory. Linear execution.** An agent-native dev tool: a skinny linear tracker +
persistent agent memory (pgvector) + request triage + a PRD editor, with native MCP tools
so agents read/write project context through the *same code path* as the web app. Runs
fully offline with `docker compose up`.

## Start here

- **[Product overview](product-overview.md)** — vision, goals, target users, philosophy, licensing
- **[Design philosophy](design-philosophy.md)** — the harness-engineering principles the codebase is built to, and how they're enforced
- **[Getting started](getting-started.md)** — install, run, first login, a guided tour
- **[Native install](native-install.md)** — launchd / systemd team server, no Docker
- **[Release](release.md)** — stamp a CalVer, publish the tarball, Install from Settings
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

## Task guides

One guide per area of the left nav that the guides above do not already cover.

| Guide | What it covers |
| --- | --- |
| [Triage](triage.md) | Working the request queue — accept, merge, decline |
| [Code graph](code-graph.md) | The described map of the repo, its health, and what work touches it |
| [Stack](stack.md) | The derived architecture map, per-component liveness and code-health overlay |
| [Rules](rules.md) | The rule library, toggles, and what each rule enforces |
| [Fleet](fleet.md) | Agents, roles, presence, claims and reviews |
| [Outposts](outposts.md) | Machines that run fleet workers |
| [Harness](harness.md) | Harness telemetry — capacity, spend and outcomes by vendor and model |
| [Lessons](lessons.md) | The published lesson catalog and how it is measured |
| [Activity](activity.md) | The project activity stream |
| [Live](live.md) | The live agent-call feed |

## Reference

| Doc | What it covers |
| --- | --- |
| [MCP tools](mcp.md) | The 78 MCP tools, JSON-RPC endpoint, API-key auth, error taxonomy, call metering |
| [Grok Build](grok-build.md) | Connect Grok Build (xAI's coding CLI) to Graphban's MCP + prime it on the loop |
| [Fleet on another machine](fleet-remote.md) | Standing a drain up on a Linux box — the vendor logins that cannot be scripted, `gbfleet service`, linger |
| [Vendor adapters](fleet-adapters.md) | The fleet vendor-adapter support matrix |
| [Supervisor acceptance walk](fleet-supervisor-walk.md) | What the PRD-22 supervisor walk actually did |
| [Delegation](delegation.md) | Standing delegation up on a project — the planner credential, the supervisor, `delegate(seat=true)` + `spawn`, and every refusal |
| [Cursor](cursor.md) | Connect Cursor to Graphban's MCP (user + Cursor 3 Team scope) + the sub-agent fleet |
| [Swamp](swamp.md) | Set up Graphban + Swamp: gate keys, CI on ubuntu-srv, `@graphban/item-lifecycle` `complete` / `probe` |
| [Swamp integration](swamp-integration.md) | Design: why the pair is stronger, the attestation port, the licensing boundary, remaining P26 gates |
| [AI providers](ai-providers.md) | The provider abstraction — stub / Ollama / Anthropic / OpenAI |
| [API reference](api-reference.md) | Every REST + public endpoint |
| [Data model](data-model.md) | Entities and relationships |
| [Configuration](configuration.md) | Environment variables |
| [Development](development.md) | Local dev, tests, migrations |
| [Deploy](deploy.md) | Self-host deploy runbook — release identity, verification, recovery, rollback |
| [Release](release.md) | Stamp CalVer, pack `graphban-<tag>.tar.gz`, GitHub Release, UI Install |
| [Releasing `gban` and `gbfleet`](releasing.md) | The two CLI packages, their tags and lifecycles |
| [Self-host exposure](self-host-exposure.md) | Exposing the feedback API to the public internet |
| [Deploy on Railway](deploy-railway.md) | The hosted multi-tenant deployment on Railway |
| [CI runners](ci-runners.md) | Our own CI machines and the workflows still on hosted runners |
| [Token census](token-census.md) | Where an agent's tokens actually go |
| [Implementation plan](IMPLEMENTATION_PLAN.md) | The original phase plan, kept as a record — see the roadmap for current status |

## Configuring a box

Four books, each owns one decision. **Environment variables** — what the process reads at
start — are in [Configuration](configuration.md). **Screens** — what an admin changes at
runtime in the UI — are in [Settings & profile](settings.md). **Rules** — what the
tracker enforces on work — are in [Rules](rules.md). **The network** — how the box is
installed, reached and exposed — is in [Deploy](deploy.md), [Native install](native-install.md)
and [Self-host exposure](self-host-exposure.md).

## Conventions used in these docs

- **UI** paths are described from the left nav / top bar.
- **API** endpoints are mostly under `/api` (e.g. `POST /api/items`), JWT-authed unless noted
  **public** (unauthenticated) or **MCP** (API-key authed). A few host routes sit outside
  it — `/health` and the public org-host paths such as `/{path_id}/feedback` — see the
  [API reference](api-reference.md).
- The app starts empty — create an account and your first project in the UI. A demo dataset
  is available opt-in via `SEED_ON_START=true` (see [Getting started](getting-started.md)).
