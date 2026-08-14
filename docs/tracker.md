# Tracker

The Tracker is the heart of Graphban: **one linear stream** of work items ordered by
priority and recency — no boards, no columns. It's the landing view.

## Item model

Each item has:

| Field | Notes |
| --- | --- |
| `id` | Human key, e.g. `AL-12` (auto-assigned `AL-<n>`) |
| `title`, `description` | Description is markdown |
| `status` | One of six states (below) |
| `tags` | Free-form list |
| `effort` | Points (integer) |
| `sort_order` | Drag-reorder position |
| `blocker` | Free text; shown as a red banner when set |
| `reporter` | Name / handle / avatar |
| `pr` | Optional PR metadata (number, branch, state, additions/deletions, checks) |
| `date` | Display date |

### States

`backlog` → `next` → `in_progress` → `review` → `done`, plus `blocked`. Each has a fixed
color used consistently across the app:

| State | Color |
| --- | --- |
| Backlog | `#8b949e` |
| Next | `#7ca2ff` |
| In Progress | `#c6f24e` |
| Review | `#e0b34a` |
| Done | `#5fd07a` |
| Blocked | `#ff6b6b` |

## Using it

- **Advance status** — click the status dot on a row (or in the detail panel) and pick a new
  state. Moving an item to **Done** triggers [auto-extraction](memory-and-chat.md#auto-extraction-on-done).
- **Reorder** — drag a row; the new order persists (`sort_order`).
- **Filter** — the quick-filter chips (All / per-state) narrow the stream; the top-bar search
  filters by title or id.
- **Detail panel** — click a row to slide in the detail: description, blocker, tags/effort,
  PR card, and **linked memory shards** (shards whose `item_id` matches).
- **New item** — the "New item" button opens a dialog (title, description, tags, effort);
  it drops into the backlog at the top of the stream.

## How it works

- Service: `backend/app/services/items.py` — `list_items`, `create_item`, `update_item`,
  `reorder_items`, `search_items`, `get_backlog`, `get_item_details`, `suggest_next`, and
  `_auto_extract_lessons` (fires on the transition to `done`).
- This service is shared by the REST routes **and** the MCP tools, so an agent's
  `create_item`/`update_item` is identical to the UI's.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/items` | List (optional `?status=` / `?project_id=`) |
| POST | `/api/items` | Create |
| PATCH | `/api/items/reorder` | Persist a new drag order (`{ordered_ids: [...]}`) |
| GET | `/api/items/{id}` | Fetch one |
| PATCH | `/api/items/{id}` | Update fields / advance status |

Front-end mutations use optimistic updates (TanStack Query) so status changes and drags feel
instant.

## Working the backlog with agents

The tracker isn't just a view — it's a work queue. Three ways to put agents on it, in
order of how you should adopt them:

### 1. The claim loop (start here)

Point any MCP-connected agent session at the backlog and tell it to work. The intended
cycle is:

```
claim_cluster → get_context / get_item_details → work
  → update_item (touchpoints, status="review") → heartbeat while working
  → claim_review someone else's work → sign_off or bounce
```

`claim_cluster` **atomically** leases a non-colliding batch and **reserves the files it
touches**, so two sessions never grab the same work — or work that collides with it.
`claim_next` still claims a single item, but it reserves nothing.

Finished work goes to `review`, not straight to `done`: another agent takes it with
`claim_review` and signs it off. **No agent can sign off what it built**, whatever role it
currently holds. A solo agent therefore leaves review to you, unless the project has
danger mode on (Settings → the project) — and even then the server refuses self-review
whenever another agent could have done it. Filter by tag to scope a session to a theme
(e.g. "work the `railway` backlog"). The tracker shows live claim state as agents run.

### 2. Parallel agents, clustered by touchpoints

Fanning out to several agents at once risks two of them editing the same files. That's
what `next_cluster` is for: it claims the best ready item **plus its related ready
items** (grouped by touchpoint overlap) in one call, giving each agent a conflict-free
neighborhood. The pattern:

- An orchestrator calls `next_cluster` per agent → conflict-free batches
- One agent per cluster, each in an isolated git worktree
- Agents `heartbeat` their items; a crashed agent's lease expires and its items are
  automatically freed for the next agent

### 3. Scheduled / autonomous

A cron-style loop that wakes up, runs the claim loop for one item, and stops when
`claim_next` returns `{claimed: false}`. Adopt this once the interactive pattern is
proven — the lease + heartbeat semantics are what make it safe to leave unattended.

See [Task claiming](mcp.md#task-claiming-safe-multi-agent-loops) and
[Code-locality clustering](mcp.md#code-locality-clustering-pick-up-related-work-at-once)
for the full semantics.

## Related

- Agents drive items through MCP: `create_item`, `update_item`, `search_items`,
  `get_backlog`, `get_item_details`, `suggest_next` — see [MCP tools](mcp.md).
- Completing an item can mint a memory shard — see [Memory & chat](memory-and-chat.md).
- Items can be linked to PRDs and to each other (typed links) — see [PRDs](prds.md) and
  [Links graph](links-graph.md).
