# Memory & agent chat

The right-hand **Agent context** sidebar (toggle with the **Agent** button in the top bar)
has two tabs: **Memory** and **Chat**. Together they make the project's knowledge
searchable and let an agent reason over it.

## Memory shards

A *shard* is a small unit of durable project knowledge — a decision, a learning, a
convention. Each has:

| Field | Notes |
| --- | --- |
| `text` | The content |
| `scope` | `global` (project-wide) or `item` (attached to an item) |
| `source` | Where it came from, e.g. `from AL-08`, `global`, `lesson from AL-16` |
| `status` | `candidate` → `published` → (`rejected`) — the trust boundary, below |
| `origin` | Who wrote it: `user:<handle>`, `agent:<key>`, or `agent:auto-extract` |
| `item_id` | Set for item-scoped shards |
| `embedding` | Vector, computed on write |
| `fresh` | Marks recently added shards |

### The trust boundary (candidate → published)

Agent memory is *telemetry, not truth*. A shard an agent writes — via `add_memory`
or auto-extracted when an item moves to Done — enters as a **candidate** and does
**not** appear in the default semantic search. A human reviews it in **Memory
review** (left nav) and either **publishes** it (promotes it into the trusted
retrieval path every future agent searches) or **rejects** it (kept for provenance,
never surfaced). Human-authored shards are published immediately. Agents can opt
into seeing unreviewed notes with `search_memory(include_candidates=true)`. Publish
and reject are recorded in **Activity**. This keeps one hallucinated note from
becoming ground truth for the next agent.

**Grilling feeds the queue.** When you apply a PRD [grill](prds.md) session, each
decision you made becomes a `candidate` shard (`origin: agent:grill`, sourced to the
PRD) — so grilling decisions land in Memory review for approval instead of
evaporating when the context is cleared.

**An item-close lesson always waits for a human (GRPH-358).** Auto-triage may auto-*reject* a
shard with `origin: agent:auto-extract` — near-duplicate cleanup keeps the queue readable —
but nothing auto-*publishes* one, in any `memory_write_mode`, including `trusted`.

The reason is worth knowing, because it is not a confidence problem. Extraction distils an
item's own text, and an item's **description is written before the work**: it holds the
proposal and the framing the build then revised. So an extracted lesson can be fluent,
specific, novel, and flatly contradicted by the code that shipped — which is exactly what
happened when GRPH-354 closed. No signal the scorer reads can catch that: similarity says
"not a duplicate", recurrence says nothing, and an LLM judge asked "is this a good lesson?"
says yes, because it reads like one. Every input agrees while the claim is false, so a higher
threshold would not have helped at any value.

Extraction now also reads the item's **evidence, PR link and final status first**, with the
description supplied after and labelled as possibly stale. That improves the input; it does
not guarantee the output, which is why the human boundary above is a hard rule rather than a
tuned one.

### Semantic search

The Memory tab's search box runs a **semantic** search (not keyword): the query is embedded
and compared to shard embeddings by cosine similarity, best-first, with a score.

- On **Postgres** this uses the pgvector `<=>` (cosine distance) operator with an ivfflat
  index.
- On **SQLite** (tests / zero-infra) it falls back to a Python cosine over the shard set.

Both paths go through the configured [embedding provider](ai-providers.md). The default
**stub** embedder is deterministic and offline; switch to Ollama/OpenAI for real semantics.

### Adding & editing

- **Add** a shard from the box at the bottom of the Memory tab (defaults to `global` scope).
- **Editing** a shard **re-embeds** it, so search stays consistent after edits (this fixes
  the classic "stale embedding after edit" bug).
- **Backfill** — after switching embedding providers, `POST /api/memory/backfill` re-embeds
  every shard with the current provider.
- **Import / export** — shards can be exported to JSON and re-imported.

### Auto-extraction on done

When a tracker item transitions to **Done**, the [Extractor](ai-providers.md) distills 1–3
durable shards from its title + description and attaches them (`source: "lesson from AL-…"`).
This is idempotent (won't double-extract) and respects the project's `auto_extract` flag.
The offline stub uses a heuristic (pulls decision/learning-flavored sentences); a real
provider generates them.

## Agent chat (streaming)

The **Chat** tab is a retrieval-grounded assistant. Each message:

1. Runs a semantic memory search for the top-k relevant shards.
2. Assembles context — project status counts, in-progress items, a suggested next item, and
   those shards.
3. Sends it to the configured **chat provider** and streams the reply back token-by-token.

Streaming uses **Server-Sent Events**: `POST /api/agent/chat/stream` returns
`text/event-stream` with a `shards` event, then `delta` events, then `done`. The SPA reads
it via `fetch` + `ReadableStream` (so it can send the JWT header) and appends deltas live.

With the offline **stub** provider the reply is a deterministic, grounded summary that names
the suggested next item and cites shards — and tells you how to enable a real model. With
**Ollama** or **Claude** configured, it's generated.

## How it works

- Service: `backend/app/services/memory.py` (shards, `search_memory`, `add_memory`,
  `update_shard`/re-embed, `backfill_embeddings`, export/import).
- Router: `backend/app/routers/agent.py` (`/chat`, `/chat/stream`) and
  `backend/app/routers/memory.py`.
- Providers: `backend/app/providers/` — `Embedder`, `ChatModel` (with `stream()`),
  `Extractor`. See [AI providers](ai-providers.md).

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/memory/shards` | List |
| POST | `/api/memory/shards` | Add (embeds on write) |
| PATCH | `/api/memory/shards/{id}` | Edit + re-embed |
| POST | `/api/memory/search` | Semantic search (`{query, top_k, project_id}`) |
| POST | `/api/memory/backfill` | Re-embed all shards |
| GET | `/api/memory/export` | Export shards as JSON |
| POST | `/api/memory/import` | Import shards |
| POST | `/api/agent/chat` | Grounded reply (non-streaming) |
| POST | `/api/agent/chat/stream` | Grounded reply over SSE |

## Related

- Agents call `search_memory` and `add_memory` over [MCP](mcp.md) — the same code path.
- `extract_lessons` and `generate_digest` MCP tools build on the memory + item services.
