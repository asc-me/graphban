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
- Dogfood-able daily; a compelling hosted SaaS tier later.

## Target users

- Solo indie hackers / founders.
- Small dev teams building AI/agent products.
- Power users of Claude Code, local LLMs, and MCP.
- Developers frustrated with bloated trackers or lost context.

## What's in the box

A dark-only, keyboard-friendly web app with these areas (each has its own guide):

- **Tracker** — one linear stream across six states, drag-reorder, inline status, detail panel.
- **Agent memory** — searchable memory shards (pgvector), auto-extraction of lessons on
  completion, a streaming AI chat sidebar grounded in project + memory context.
- **Requests** — a triage queue plus a public embeddable feedback form with auto-duplicate
  detection, and a Feedback Kit that generates a themeable widget.
- **PRDs** — a list and a markdown editor with live preview, version history + diff, and AI
  drafting commands.
- **Links / Code graph / Dashboard / Roadmap / MCP Tools** — a typed-relationship graph, an
  agent-described code map, project-health widgets, a phased roadmap (with a public share
  link), and a live view of the MCP surface.
- **Activity** — an audit ledger: every accepted mutation, attributed to the agent key or
  user that made it.
- **Settings / Profile** — switch AI providers, connect integrations, configure the project,
  manage members and API keys.
- **Native MCP** — 55 tools over a JSON-RPC endpoint, authenticated by scoped API keys, with
  a typed error taxonomy (every failure carries a machine code + repair hint).

## Non-goals (current)

- Full Kanban boards or complex Gantt.
- Advanced analytics / time tracking.
- Native mobile apps (responsive web first).
- Enterprise SSO / on-prem enterprise features.
- A hosted multi-tenant service (a later, additive layer — see the implementation plan).

## Technical stack

- **Frontend** — React 19 + TypeScript + Tailwind v4 + TanStack Query, shadcn-style UI.
- **Backend** — FastAPI + SQLAlchemy.
- **Database** — PostgreSQL + pgvector (SQLite fallback for tests / zero-infra dev).
- **AI** — provider-agnostic (offline stub by default; local Ollama or cloud Claude/OpenAI).
- **Auth** — JWT for the web app + scoped API keys for MCP/agents.
- **Deployment** — Docker Compose for one-command self-host.

## Licensing & business model

- **License** — [Functional Source License 1.1 (FSL-1.1-Apache-2.0)](../LICENSE.md). Free to
  use, modify, and self-host for personal, internal, and development use. "Competing Use" —
  reselling the software or offering it as a hosted/SaaS service that substitutes for it — is
  not permitted without a commercial license. Each version converts to Apache-2.0 two years
  after release.
- **Hosted tiers (future, TBD)** — free (limited projects/memory), Pro, Team/Enterprise.
