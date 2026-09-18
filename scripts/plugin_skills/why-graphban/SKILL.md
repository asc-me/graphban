---
name: why-graphban
description: Source playbook for pstack /why investigators. Classifies Graphban as five categories, not one.
---

# Graphban — `/why` source playbook

Graphban is **not** a Linear-shaped issue tracker. It is five categories of surface, each
served by different MCP tools. A `/why` investigation that files Graphban under "issue tracker"
and stops is the defect this playbook exists to prevent.

For each category below, there are **three states**:

| State | Meaning | Action |
|---|---|---|
| **Results** | Tool returned data | Report findings |
| **Empty** | Tool worked, returned nothing | This is a **finding** — report it as such |
| **Unavailable** | Tool error, MCP unreachable, server down | Skip-with-reason (unavailable) — the skip `/why` already allows |

**The load-bearing distinction:** *unavailable* (tool error) and *empty* (finding) are NOT
the same. A tool that works and returns nothing is a finding. A tool that cannot run is a
skip. Do not treat them interchangeably.

---

## Category 1: Issue / ticket tracker

Items, claims, evidence, Activity.

**Tools:** `search_items`, `get_item_details`, `get_backlog`

- `search_items` — free-text query across title, description, tags, status.
- `get_item_details` — full record for one item: description, blockers, dependencies, linked
  memory shards, brief (with lane/tier suggestions and basis).
- `get_backlog` — prioritized ready-first view with composite scores, blocked_by, unblocks.

**Empty is a finding.** Zero items matching a query means the work is not tracked, not that
the tracker is quiet. Report it.

---

## Category 2: Long-form documents (PRDs)

PRDs are the intent layer — the *why* behind the items.

**Tools:** `get_prd`, `prd_coverage`, `prd_acceptance`

- `get_prd` — full PRD including markdown body. Read before concluding what a PRD says.
- `prd_coverage` — spec-to-task rollup: gaps (no tasks), empty_sections (no substance),
  shaped (false = not a clean pass).
- `prd_acceptance` — delivery acceptance. `view=completeness` surfaces ABSENT (nothing
  delivered) vs UNDELIVERED (attempted but not done).

**Empty is a finding.** A PRD with no coverage means no one decomposed it. A coverage report
with `shaped: false` means the spec has no substance. These are findings, not "nothing to
report."

---

## Category 3: Memory

Shards and lessons — decisions, corrections, project knowledge that persists across sessions.
pstack's seven-bucket map has no slot for this; name it separately, do not fold it into
tickets. `related_work` is the prior-attempt / neighborhood surface — also not a ticket search.

**Tools:** `search_memory`, `related_work`, `get_lessons`, `extract_lessons`

- `search_memory` — semantic search over memory shards. Published shards ranked by
  similarity (score, status). `include_candidates: true` adds unreviewed shards.
- `related_work` — items related to a task by shared touchpoints and typed links (the
  prior-attempt and code-neighborhood surface). Do not fold this into the issue-tracker
  category; it is not a Linear search.
- `get_lessons` — published lesson catalog with effectiveness, caught-issues, and
  org-eligibility. `score null` / `candidate` = not yet judged.
- `extract_lessons` — distil lessons from a session into memory (async; see linked_shards).

**Empty is a finding.** No memory shards for an area means nothing was learned or recorded.
Empty `related_work` means no neighborhood is linked — report it, do not treat it as "no
tickets." Empty memory is a named finding, not "the memory system is not in use."

---

## Category 4: Code graph

Structure of the codebase as a graph of nodes and typed edges — what depends on what, what
touches what.

**Tools:** `get_code_map`, `code_neighbors`, `search_code`, `graph_query`

- `get_code_map` — the project's code graph: described nodes (path, kind, summary, fresh)
  and typed edges. Max 200 nodes unless `limit:0`; check `truncated`.
- `code_neighbors` — the neighborhood around a code path: incoming/outgoing edges by type
  plus work items touching it.
- `search_code` — semantic search over code-node summaries (pgvector cosine). Returns ranked
  nodes with scores.
- `graph_query` — structural queries: `hubs` (nodes by inbound edges), `components`
  (connected groups), `centrality`.

**Empty is a finding.** An empty code map means the codebase structure was never described.
A search returning zero nodes means nothing in the graph matches — report it.

---

## Category 5: Live

Who holds what *right now* — real-time agent presence, claims, leases.

**Tools:** `fleet_status`

- `fleet_status` — who is online, what role each holds, what they are holding, who has gone
  `offline` or been `quarantined`. Presence is derived from last contact, so an agent that
  died reads `offline` here without anything reporting it.

**Empty is a finding.** Live with zero agents is the unreserved class — it means nobody is
working, not that the server is down. Report it as a finding: "no agents currently active."
Do NOT treat empty Live as transient or unavailable.

---

## Summary

| Category | Tools | Empty means |
|---|---|---|
| Issue tracker | `search_items`, `get_item_details`, `get_backlog` | Work is not tracked |
| PRDs | `get_prd`, `prd_coverage`, `prd_acceptance` | No spec or no decomposition |
| Memory | `search_memory`, `related_work`, `get_lessons`, `extract_lessons` | Nothing learned, linked, or recorded |
| Code graph | `get_code_map`, `code_neighbors`, `search_code`, `graph_query` | Structure never described |
| Live | `fleet_status` | Nobody is working |

A missing category in a `/why` report is itself a finding. Every category must be addressed —
either with results, with an explicit "empty" finding, or with a skip-with-reason
(unavailable).
