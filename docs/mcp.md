# MCP tools

Graphban exposes a **Model Context Protocol** surface so agents can read and write project
context. The key property: **MCP tools call the same service layer as the web app**, so an
agent's writes are identical to a user's and appear instantly in the UI.

## Endpoint & protocol

- **`POST /api/mcp`** — JSON-RPC 2.0 over HTTP. Handles `initialize`, `tools/list`,
  `tools/call`, and the `notifications/initialized` notification. Single JSON responses (no
  SSE) keep it `curl`-friendly while remaining MCP Streamable-HTTP compatible for simple
  calls. The server advertises MCP protocol **2025-11-25** and negotiates down to a
  client's requested version when it's one it supports.
- **Auth** — a scoped **API key** via `X-API-Key: gb_sk_…` or `Authorization: Bearer
  gb_sk_…`. Create one in [Settings → API Keys](settings.md#api-keys-tab). Unauthenticated
  calls return `401`.
- **Project scope** — each key targets one **project** by default (the active project when
  you create it), so an agent's writes land in the right workspace. Tick *global* at creation
  to leave it unscoped. Any project-scoped tool call (`create_item`, `add_memory`,
  `search_items`, `search_memory`, `get_backlog`, `suggest_next`, `generate_digest`,
  `describe_code`, `get_code_map`, `code_neighbors`, `search_code`) also accepts an optional
  `project_id` argument that overrides the key's project for that call.
- **Metering** — every `tools/call` increments a per-tool counter (the `mcp_tool_stats`
  table) surfaced on the **MCP Tools** view.
- **Scope-gated manifest** — `tools/list` returns only the tools the key can call: a
  read+write key sees all 32, a read-only key sees just the read tools. A read-only key
  never pays for write-tool schemas it would only get `unauthorized` on (AL-78).
- **Lean list rows** — `search_items` and `get_backlog` return a compact row
  (`id`/`title`/`status`, plus the ranking fields for the backlog) by default. Pass
  `fields: "full"` for every item field, or call `get_item_details` once you pick one.

## Connecting clients

**Settings → API Keys** generates ready-to-paste snippets for every supported client the
moment you create a key (the plaintext is shown once). Supported clients and where their
config lives:

| Client | Config | Shape |
| --- | --- | --- |
| Claude Code | `claude mcp add` (CLI) | `--transport http … --header "X-API-Key: …"` |
| Cursor | `~/.cursor/mcp.json` | `mcpServers.<name>.{url, headers}` — full guide: [cursor.md](cursor.md) |
| Codex | `~/.codex/config.toml` | `[mcp_servers.<name>]` with `url` + `http_headers` |
| opencode | `opencode.json` | `mcp.<name>.{type: "remote", url, headers}` |
| Hermes | `~/.hermes/config.yaml` | `mcp_servers.<name>.{url, headers}` |
| OpenClaw | `~/.openclaw/openclaw.json` | `mcp.servers.<name>.{url, transport, headers}` |
| Grok Build | `grok mcp add --transport http …` (`~/.grok/config.toml`) | streamable-HTTP, direct — see [grok-build.md](grok-build.md) |

**Hermes** — add under `mcp_servers` in `~/.hermes/config.yaml`, then run `/reload-mcp`:

```yaml
mcp_servers:
  graphban:
    url: "https://<your-host>/api/mcp"
    headers:
      X-API-Key: "gb_sk_…"
    enabled: true
```

**OpenClaw** — add under `mcp.servers` in `~/.openclaw/openclaw.json` (or one-shot via
`openclaw mcp set graphban '<json>'`), then verify with `openclaw mcp doctor --probe`:

```json
{
  "mcp": {
    "servers": {
      "graphban": {
        "url": "https://<your-host>/api/mcp",
        "transport": "streamable-http",
        "headers": { "X-API-Key": "gb_sk_…" }
      }
    }
  }
}
```

Every client authenticates the same way: the key in an `X-API-Key` header (or
`Authorization: Bearer`), against a URL reachable **from where the agent runs**.

## The 52 tools

The manifest you receive is gated twice. **By scope** (AL-78): a key without `write` is not
shipped the mutating tools it would only be refused on. **By role** (PRD-17 D-b): a key whose
`roles` name a single role is not shipped the other roles' tools — a reviewer credential
carries no `claim_next`, a worker credential no `sign_off`. A single-role fleet key sees
roughly 16–19% fewer tokens.

`wait_seconds` (max 60) parks a claim instead of returning empty — one tool call a minute
rather than twelve. The park holds no database transaction while idle, and an outstanding
`directive` wakes it early, so a re-tasked agent adopts its new role in seconds rather than at
timeout.

Gating reads the KEY's eligible roles, not the agent's *active* role. A ceiling is fixed at
mint, so the manifest cannot go stale; an active role changes under a live connection, and
this endpoint has no channel to push a replacement. Either way the **call gate** is the
enforcement point — a manifest can only fail to mention a tool, while the gate refuses it.

| Tool | Params | Does |
| --- | --- | --- |
| `get_context` | — | Orient: the key's project, scopes, project/tool counts. Call this first. |
| `list_projects` | — | All projects (`id`, `name`, `tag`, `accent`, `description`) — ids for the `project_id` override; `tag` is the short prefix its item/request/PRD keys render with |
| `create_project` | `name`, `tag`, `description` | Create a project so `setup_project` has somewhere to bootstrap. **Self-host only, and only while unlinked** — refused in hosted mode and once the instance is linked to a cloud org, since a project created here would reach that org's tenant (AL-284) |
| `setup_project` | `project_id` | **First-run bootstrap** — an ordered, resumable checklist (confirm project → build graph → load memories → propose items). Read-only; call it when `get_context` reports an empty project |
| `next_cluster` | `agent_id`, `max_items`, `project_id` | **Claim a code-neighborhood at once** — the best ready item plus its related ready items, all assigned to you. |
| `related_work` | `id` | Items related to a task by shared touchpoints + typed links, best-first (read-only) |
| `claim_next` | `agent_id`, `lease_seconds`, `wait_seconds`, `project_id` | **Atomically** claim the best ready item, assign it to you, move it to in_progress. Returns `{claimed, item}`. |
| `propose_allocation` | `project_id` | What the fleet should look like given who is online and what is ready. A proposal — nothing is assigned until `assign_role` |
| `assign_role` | `agent_id`, `role`, `reason` | Commit a role change; it reaches the agent on its next poll as a `directive`, with no reconnect |
| `mint_enrolment` | `agent_id`, `role`, `wave` | PLANNER ONLY. Mint a seat for an agent you are spawning, bounded by your credential — the code is returned once and grants that role for one session |
| `collision_clusters` | `project_id`, `status` | Partition ready work into clusters that provably share no touch-areas; `predicted` marks lower-confidence grouping |
| `claim_cluster` | `agent_id`, `max_items`, `lease_seconds`, `wait_seconds` | Claim a whole non-colliding cluster and reserve its areas, checked against in-flight work |
| `claim_review` | `agent_id`, `project_id`, `wait_seconds` | Lease an item in review you did **not** build, and are independent of — not your own call tree, and not the same credential on the same host |
| `sign_off` | `id`, `agent_id`, `evidence` | Take a reviewed item to `done`. Refused if you built it — and, above effort 3, refused without a `sabotage` receipt |
| `bounce` | `id`, `agent_id`, `reason` | Send it back to `next` with a reason, reserved for its author for one lease period |
| `register_agent` | `label`, `capabilities`, `worktree`, `branch`, `role_hint`, `parent_agent_id` | Register THIS process as an agent and learn its role. Two terminals on one key become two agents. Returns `{agent_id, key, active_role, enrolled, tools_off_limits, heartbeat_interval_seconds}` — `tools_off_limits` names the tools this role will be refused, which the manifest cannot, having been fetched before the role existed |
| `fleet_status` | `project_id` | Who else is working this project: agents, roles, derived presence, and what each holds |
| `heartbeat` | `id`, `agent_id` | Extend the lease on an item you hold **and** your agent presence (so neither is reclaimed while you work) |
| `release_item` | `id`, `agent_id`, `to_status` | Return a claimed item to the queue |
| `create_item` | `title`, `description`, `tags`, `touchpoints`, `effort`, `status`, `fidelity`, `project_id` | Create a tracker item (returns its `project_id`) |
| `update_item` | `id`, `status`, `title`, `description`, `tags`, `touchpoints`, `effort`, `blocker`, `fidelity`, `prd_id`, `prd_section` | Patch / advance an item |
| `search_items` | `query`, `tags`, `status`, `fields`, `project_id` | Query the stream (query matches title, description, **and** tags); lean rows by default, `fields="full"` for all |
| `add_memory` | `text`, `scope`, `item_id`, `project_id` | Record a memory shard. Resolved `status` follows the project's memory write mode: `review` → **`candidate`** pending human publish (AL-49, the default), `auto` → published only when strongly corroborated, `trusted` → published on write so an agent can read its own writes back (AL-280) |
| `publish_memory` | `shard_id` | **Submit** a candidate for independent adjudication — the judge decides, not the caller. Returns `{shard, verdict}`; `kept: false` is a normal outcome. Needs `agent_adjudication` on the project **and** a real chat model, else `unavailable` and the shard is untouched (AL-282) |
| `reject_memory` | `shard_id`, `reason` | Discard your own candidate. No judge needed — it removes nothing from the trusted pool (AL-282) |
| `search_memory` | `query`, `top_k`, `include_candidates`, `project_id` | Semantic search over **published** shards (set `include_candidates` for unreviewed ones); returns `status`, `item_id`, `source` |
| `get_backlog` | `limit`, `fields`, `project_id` | Prioritized backlog (lean rows + ranking fields by default, `fields="full"` for all) |
| `get_item_details` | `id` | Item + linked shards + linked requests |
| `suggest_next` | `project_id` | Best next item from state + memory |
| `link_items` | `a`, `b`, `type`, `reason` | Create a typed relationship |
| `unlink_items` | `a`, `b`, `type` | Remove a typed relationship (inverse of `link_items`); omit `type` to remove all types for the pair. Idempotent — returns `removed` |
| `extract_lessons` | `id` | Distill lessons from an item into memory |
| `generate_digest` | `project_id` | Compose a progress digest across the project |
| `prd_coverage` | `prd_id` | Spec-to-task rollup: per-section counts, coverage %, gaps (read-only) |
| `decompose_prd` | `prd_id`, `create` | Propose (or create) one task per un-covered PRD section |
| `create_prd` | `title`, `body`, `template`, `project_id` | **Author a PRD** (the handoff artifact) — `## ` sections drive decompose/coverage |
| `update_prd` | `prd_id`, `title`, `status`, `body` | Patch a PRD's title, status (`draft`/`review`) or body. **`approved` is not settable** — it is reached by finishing the grill; setting it returns `conflict` naming what is still outstanding (AL-300) |
| `answer_grill` | `prd_id`, `answer` | Relay the author's answer to a grill question — recorded as **agent-relayed**, visible to whoever reviews later. Returns `outstanding` + `complete` (AL-299) |
| `grill_prd` | `prd_id` | **Grill** — next clarifying questions to sharpen a PRD before building (read-only) |
| `describe_code` | `nodes`, `edges`, `prune`, `project_id` | **Record code structure** — upsert code nodes (module/file/symbol + summary) and typed edges. Idempotent by path; re-describe on change |
| `get_code_map` | `kind`, `project_id` | The project's code graph — described nodes + typed edges (read-only) |
| `code_neighbors` | `path`, `project_id` | Edges around a path (in/out by type) + work items touching it (read-only) |
| `search_code` | `query`, `top_k`, `project_id` | Semantic search over code-node summaries (read-only) |
| `link_code` | `ref_id`, `path`, `relation`, `ref_type`, `project_id` | **Bridge a tracker item/request to a code path** (affects/implements/fixes/tests/references). Idempotent; surfaces both ways |
| `unlink_code` | `ref_id`, `path`, `relation`, `project_id` | Remove an item/request ↔ code link |
| `prd_acceptance` | `prd_id`, `view` | **Delivery acceptance, read-only** — one surface per `view`: `completeness` (baselined sections with nothing delivered — the only pass that surfaces ABSENT work), `drift` (mechanical, total preserved across a rebaseline), `evidence` (receipts split by whether anyone but their author could check them, + code-graph corroboration), `close_report` (delivered vs **original** intent), `readiness`, `lineage`, `verdicts`, `baseline`, `classifications` (the platform judge's serves/enables/unrelated per completed item), `audit_brief` (everything a repo-holding agent needs to audit this PRD in one read — intent text, linked work with its classifications, receipts, and what is outstanding), `audit_coverage` (which sections actually carry a verdict). None of them say "complete" |
| `request_rebaseline` | `prd_id`, `reason_type`, `reason` | **Ask for new frozen intent** in your own words. Does NOT approve — it re-opens the grill, and the existing baseline keeps governing until a new one is earned. Cannot add sections (that is a sub-PRD); refused on a closed PRD |
| `submit_verdict` | `prd_id`, `section`, `outcome`, `citations`, `reasoning` | **Record a sign-off claim with provenance.** Citing nothing, or citing something that does not resolve, is rejected as malformed. Citations are `{kind, ref}` — `code`, `intent` (what an ABSENCE finding cites), or `evidence`. Signing your own claimed work is flagged, not refused |
| `close_prd` | `prd_id`, `dispositions`, `verdict` | **Close a PRD** — terminal, irreversible. Gates on **disposition**, never on delivery: every section with nothing delivered must be promoted or deferred with a reason. Post-close work becomes a new PRD |
| `learning_loop` | `view`, `id`, `project_id` | **The learning loop, read-only** — `recommendations` (pending artifact proposals), `artifact` (one, with its draft and install plan), `usage` (population + uses; **null**, never 0, for a tier whose use cannot be observed), `stale` (only observable tiers — zero uses elsewhere is not evidence of disuse) |
| `review_recommendation` | `id`, `decision` | **Approve or reject a proposed artifact** — the human boundary. Approving writes nothing: a `shared_surgery` artifact is only ever proposed, with its contents returned for a human to apply |
| `report_graphban_issue` | `type`, `title`, `detail` | Report a bug/idea about **Graphban itself** (not your project) upstream; deduped on arrival. The retired name `report_agentledger_issue` still dispatches but is not advertised |

Arguments are validated against each tool's `inputSchema` **before dispatch**, so a
missing required field or a bad enum comes back as an actionable error rather than a
crash or a silently-accepted junk value. Tool failures return `isError: true` with a
machine-readable `structuredContent.error.code`, a human `message`, and often a
`hint` naming the fix — never a raw HTTP 500:

| Code | Meaning | Agent's move |
| --- | --- | --- |
| `validation` | malformed args: missing required field, bad enum, wrong type, unknown tool | fix the args per the `hint` |
| `not_found` | a referenced id doesn't exist or isn't visible | `hint` points at `search_items` / `tools/list` |
| `conflict` | collides with state: lost lease, reused idempotency key, upstream down | usually needs fresh work or a retry later |
| `unauthorized` | authenticated but out of scope for the project/operation | retry won't help — needs a different key or a membership grant |
| `internal` | unexpected server fault | safe to retry once; if it persists, report it |

A malformed request body returns a JSON-RPC parse error (`-32700`); an unknown method returns `-32601`. An `idempotency_key` is scoped to the tool that first used it — reusing it for a different tool is a `conflict`, not a silent duplicate.

**Authority.** A key is bounded by its declared `scopes` (read/write) **and** its
owner's project memberships — a key can never out-rank the user who minted it. A
project-scoped key is further pinned to that project; the `project_id` argument
selects among in-scope projects but cannot escape the scope. Call `get_context`
first: it reports `readable_projects` and `writable_projects` for the key.

### Spec → task traceability

Items can link to a **PRD + section** (`prd_id` / `prd_section` on `create_item`/`update_item`),
so the spec and the tracker stay joined. `decompose_prd(prd_id, create=true)` proposes one
tracked task per un-covered PRD section (the gaps) and, with `create`, creates them as backlog
items linked back to the section — the spec drives the tracker. `prd_coverage(prd_id)` returns
the per-section rollup (task counts by status, `percent_done`, and `gaps`) so an agent knows
what's specced-but-unbuilt. Completing an item then updates coverage; ask `get_backlog`/
`next_cluster` for what to pick up next — the loop.

### Dependency-aware prioritization

Readiness comes from the **dependency graph**, not just the free-text `blocker`: create a
`dependency` link (`link_items(a, b, type="dependency")` — *a depends on b*) and `a` stays
**blocked until `b` is done**. `claim_next` / `next_cluster` / `suggest_next` never hand out a
blocked item, and `get_backlog` ranks **ready-first**, then by a composite score — status,
**dependency fan-out** (items that unblock many rank higher), **request votes** rolled onto the
linked item, effort, and staleness. Each `get_backlog` row carries `ready`, `blocked_by`,
`unblocks`, `votes`, and `score`, so an agent can plan against the real graph.

### Code-locality clustering (pick up related work at once)

Give items **touchpoints** — the files/globs/modules they affect (`backend/app/routers/*`,
`web/src/lib/api.ts`, a symbol name) — on `create_item`/`update_item`. Two items relate when
their touchpoints overlap (exact, glob, or same directory), and sharing a touchpoint
**auto-creates a `code` link** between them. Then:

- `related_work(id)` shows the code-neighborhood around a task (shared touchpoints + link types),
  best-first — read-only.
- `next_cluster(agent_id, max_items)` **claims the whole neighborhood in one call**: the best ready
  item plus its related ready items, all assigned to you. This is how an agent pulls several
  pieces of related work simultaneously instead of context-switching.

### Code structure graph (agent describes the codebase)

Touchpoints link *work* to files. The **code graph** is the layer above: the code's own
structure and relations, described once and queried by many. It's a set of **nodes**
(module / file / symbol, each with a one-paragraph summary, embedded for semantic search)
joined by typed **edges** (`imports` / `calls` / `owns` / `tested_by` / `references`).

- **Producer / consumer split.** The external **coding agent is the producer** — it has the
  real repo in context, so it's the source of truth. It calls `describe_code(nodes, edges)`
  as a byproduct of the work it's already doing. Graphban's **connected LLM is the
  consumer** — `search_code`, `code_neighbors`, and `get_code_map` are what it (and the UI)
  read to reason about the codebase without holding a checkout. `POST /api/agent/code` is
  that consumer wired up: it grounds the ChatModel in the graph to answer codebase questions
  in natural language (see [API reference](api-reference.md)).
- **Idempotent by path + staleness.** `describe_code` upserts by `(project_id, path)`, so
  re-describing a file after you change it updates in place — pass its new `content_hash` and
  the node is marked `fresh` again. A `describe_code(..., prune=true)` pass marks any node it
  *didn't* see as stale (`fresh=false`) instead of deleting it, so a partial describe never
  loses history. This is what keeps the map from rotting into confidently-wrong structure.
- **Reuses touchpoints for item↔code.** `code_neighbors(path)` intersects live item
  touchpoints rather than storing a second copy of the relation — "what work touches this
  module" and "what code this item affects" stay one source of truth.
- **Explicit work↔code bridge.** Beyond fuzzy touchpoint matching, `link_code(ref_id, path,
  relation)` records a curated, typed link from a tracker **item or request** to a code path
  (`affects` / `implements` / `fixes` / `tests` / `references`). It surfaces both ways:
  `code_neighbors` returns `linked_items` + `linked_requests`, and the item/request shows its
  linked code (`GET /api/agent/code/for`). This is what turns the graph into the bug/feature
  impact map — "which open bugs touch this module", "what code this feature implements".

### Task claiming (safe multi-agent loops)

Run an agent as a loop: `claim_cluster` **atomically** leases a non-colliding batch of ready
work and **reserves the areas it touches**, so no other agent is handed work that collides with
yours. `claim_next` still exists and claims a single item, but it reserves nothing — in a fleet
that difference is the whole point of the divvy.

An optimistic `claimed_by` guard means **two agents never claim the same item**. `agent_id`
defaults to the API key's name, so one key = one agent. While working, call `heartbeat(id)` to
keep the lease; if you go silent past `lease_seconds` (default 600) the item becomes
reclaimable, so a crashed agent's work is automatically freed and picked up by another.
`release_item(id)` hands it back.

Finished work goes to `review`, not straight to `done`: `update_item(id, status="review")`.
Another agent takes it with `claim_review` and calls `sign_off` (which auto-extracts lessons to
memory) or `bounce(id, reason)`. **No agent can sign off work it built** — the server checks
authorship, not the caller's current role, so no re-tasking launders it. A bounced item returns
to `next` reserved for its author for one lease period, then opens to the fleet.

A bounce reason is required, and it travels with the item: the author reads it on
`get_item_details` after reclaiming.

### Built for agents

- **Typed results** — every tool returns `structuredContent` (a JSON object) alongside the
  text block, so you consume typed data instead of parsing JSON out of prose. List/search
  tools wrap their rows under `results`.
- **Annotations** — each tool carries `readOnlyHint` / `destructiveHint` / `idempotentHint`,
  so you can tell a safe read from a mutation.
- **Idempotent creates** — pass an `idempotency_key` to `create_item` / `add_memory` /
  `link_items`; a retried call with the same key returns the original resource, never a
  duplicate.
- **Pagination** — `search_items` and `get_backlog` take `limit` + `offset` and return
  `{results, total, limit, offset, has_more}`; `search_memory` returns `{results, returned,
  top_k}`.

## MCP Tools view

The **MCP Tools** view (`/mcp-tools`) is a live card grid of all tools: name, `LIVE` status,
**call count**, description, and params. The header shows `N tools live · total calls`,
matching the "MCP · N TOOLS LIVE" chip in the top bar (N is dynamic — the live tool count). Data comes from
`GET /api/mcp/tools`.

## Examples

`tools/list`:

```bash
curl -s http://localhost:8000/api/mcp -H "X-API-Key: gb_sk_..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

`tools/call` — create an item (appears immediately in the Tracker):

```bash
curl -s http://localhost:8000/api/mcp -H "X-API-Key: gb_sk_..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"create_item","arguments":{"title":"From an agent","effort":2}}}'
```

`search_memory` returns compact JSON the agent can chain:

```bash
… -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"search_memory","arguments":{"query":"pgvector self-host","top_k":3}}}'
```

Tool results come back as MCP `content` blocks (`{"content":[{"type":"text","text":"…"}]}`);
the text is JSON for structured tools. Tool errors return `isError: true` rather than a
JSON-RPC error.

## How it works

- `backend/app/mcp_server.py` — the endpoint, the `TOOLS` schema list, and `_call_tool`
  dispatch into `services/*`. `GET /api/mcp/tools` (in `routers/analytics.py`) exposes the
  schemas + live call counts.
- The same `services/items.py`, `services/memory.py`, `services/links.py`,
  `services/insights.py` power both MCP and the REST routes.

## Related

- [AI providers](ai-providers.md) — `search_memory`, `extract_lessons`, and `generate_digest`
  use the embedding/extraction providers.
- [Settings → API Keys](settings.md#api-keys-tab) — issuing agent credentials.
