# Product overview

**Graphban** is a semi-open-source, lightweight, agent-native development tool that
combines **persistent agent memory** with a **skinny linear project tracker** and
integrated **feature/bug request management**.

It is purpose-built for solo developers, small teams, and agentic workflows — eliminating
context loss across projects while staying fast and focused. Unlike heavy tools (Jira,
Linear, Notion), it prioritizes **linearity, memory intelligence, and native MCP
integration** so AI agents can read/write context seamlessly.

## Core philosophy

- **Skinny by default** — a single linear flow, minimal states.
- **Memory-first** — every item feeds long-term recall.
- **Agent-native** — first-class MCP tools; agents and the UI share one code path.
- **Privacy & control** — local-first capable, self-host friendly, offline by default.

The engineering principles behind these — one source of truth, enforced authority,
tools that teach their own repair, proof in the real environment — are written up in
the **[design philosophy](design-philosophy.md)**.

## Goals

- Provide a single source of truth for dev items, decisions, and feedback.
- Let AI agents deeply understand and act on project context via MCP.
- Reduce context-switching and knowledge loss across projects.
- Dogfood-able daily, on a box you run yourself (self-host). There is no hosted offering: every customer runs their own box, and the cloud is the console for the org plane.

## Target users

- Solo indie hackers / founders.
- Small dev teams building AI/agent products.
- Power users of Claude Code, local LLMs, and MCP.
- Developers frustrated with bloated trackers or lost context.

## What's in the box

A dark-only, keyboard-friendly web app. The left nav groups the project areas under
**Plan**, **Build** and **Observe**, with Dashboard above them and Feedback Kit, Usage and
Settings below. Not every area has its own guide; [docs/README.md](README.md) lists the
guides that exist.

- **Plan** — **Triage** (overlaps and duplicates before work starts), the **Tracker**
  ([guide](tracker.md)), **Requests** (a triage queue plus a public embeddable feedback form
  with auto-duplicate detection), **Specs** (PRDs: a markdown editor with live preview,
  version history + diff, and AI drafting commands), and the **Roadmap** (a phased roadmap
  with a public share link).
- **Build** — **Links** (a typed-relationship graph), **Code graph** (an agent-described code
  map), **Stack**, **Rules**, **MCP Tools** (a live view of the MCP surface), **Fleet** and
  **Outposts**.
- **Observe** — **Memory** (searchable shards on pgvector, auto-extraction of lessons on
  completion), **Lessons**, **Activity** (an audit ledger: every accepted mutation,
  attributed to the agent key or user that made it), **Live** and **Harness**. A streaming AI
  chat sidebar is grounded in project + memory context.
- **Dashboard / Feedback Kit / Usage / Settings** — project-health widgets, a generator for a
  themeable feedback widget, usage, and AI providers, integrations, project config, members
  and API keys.
- **Tracker** — the default layout is **Board**; **List** is the other. Both show the same six
  statuses, with drag-reorder, inline status and a detail panel.
- **Native MCP** — 78 tools over a JSON-RPC endpoint, authenticated by scoped API keys, with
  a typed error taxonomy (every failure carries a machine code + repair hint).

## Non-goals (current)

- Custom boards or Gantt. The six statuses have a Board layout (the default); what is out is user-defined boards and timeline views.
- Advanced analytics / time tracking.
- Native mobile apps (responsive web first).
- Enterprise SSO / on-prem enterprise features.

## Technical stack

- **Frontend** — React 19 + TypeScript + Tailwind v4 + TanStack Query, shadcn-style UI.
- **Backend** — FastAPI + SQLAlchemy.
- **Database** — PostgreSQL + pgvector (SQLite fallback for tests / zero-infra dev).
- **AI** — provider-agnostic (offline stub by default; local Ollama or cloud Claude/OpenAI).
- **Auth** — JWT for the web app + scoped API keys for MCP/agents.
- **Deployment** — Docker Compose for one-command self-host. The hosted multi-tenant service is a separate deployment ([Railway](deploy-railway.md)).

## Licensing & business model

- **License** — [Functional Source License 1.1 (FSL-1.1-Apache-2.0)](../LICENSE.md). Free to
  use, modify, and self-host for personal, internal, and development use. "Competing Use" —
  reselling the software or offering it as a hosted/SaaS service that substitutes for it — is
  not permitted without a commercial license. Each version converts to Apache-2.0 two years
  after release.
- **Hosted plans** — `free`, `pro`, `team`, and `enterprise` (`backend/app/services/quotas.py`). Quotas apply when hosted mode is on. Assigning a plan is an operator action on the control plane. Self-host does not require billing.
