# MCP tools

Graphban exposes a **Model Context Protocol** surface so agents can read and write project
context. The key property: **MCP tools call the same service layer as the web app**, so an
agent's writes are identical to a user's and appear instantly in the UI.

## Endpoint & protocol

- **`POST /api/mcp`** — JSON-RPC 2.0 over HTTP. Dual-era (GRPH-223): handles
  `server/discover`, `initialize`, `tools/list`, `tools/call`, and the
  `notifications/initialized` notification. Single JSON responses (no
  SSE) keep it `curl`-friendly while remaining MCP Streamable-HTTP compatible for simple
  calls. A **modern-era** (protocol **2026-07-28**) request carries its version in
  `params._meta["io.modelcontextprotocol/protocolVersion"]` plus a matching
  `MCP-Protocol-Version` header, and is answered statelessly: `server/discover` for
  capabilities, `resultType` on every result, cache hints (`ttlMs`/`cacheScope`) on list
  responses, and `serverInfo` in the result `_meta`. A **legacy-era** client sees no
  change: `initialize` negotiates within **2025-03-26 / 2025-06-18 / 2025-11-25** (never
  echoing a modern version — a handshake selects legacy semantics), and replies keep their
  pre-adoption shape. Modern rejections: unsupported version → `400`/`-32022` listing
  `supported`; header/body mismatch or missing `Mcp-Method`/`Mcp-Name` → `400`/`-32020`;
  unknown method → `404`/`-32601`.
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

## Tool tiers

**By tier** (GRPH-571). The manifest had five tokens of headroom, so the next tool that
needed a field had nowhere to go. A key is now minted with optional **tool tiers**, and the
default is the *core* manifest — a core set of 34, ~8.5k tokens against ~13.6k untiered, so a plain
agent carries ~5,100 fewer tokens on every turn.

| Tier | What it is for |
| --- | --- |
| *(core)* | Everything on the path of doing one piece of work: finding, claiming, updating and releasing items, reviewing, memory, all code-graph **reads**, and reading a PRD |
| `prd` | **Authoring** a spec — `create_prd`, `update_prd`, `grill_prd`, `answer_grill`, `decompose_prd`, `close_prd`, `request_rebaseline`, `submit_verdict` |
| `codegraph` | **Writing** the code graph — `describe_code`, `link_code`, `unlink_code` |
| `fleet` | **Running** a fleet — `propose_allocation`, `assign_role`, `mint_enrolment`, `retire_wave`. Being *in* a fleet needs none of it |
| `misc` | `extract_lessons`, `generate_digest`, `report_graphban_issue`, `learning_loop`, `create_project`, `publish_memory`, `reject_memory` |

Two things to know, because getting either wrong is expensive:

- **A tool left out of your manifest is not forbidden.** Tiering decides what is *advertised*,
  never what may be *called* — scope and role decide that. Call a tiered-out tool on an
  authorised key and it runs. So the symptom of a missing tier is an agent that does not know a
  tool exists, never an error.
- **`get_context` tells you what you are missing.** It returns `missing_tiers` — each tier's
  name, purpose and tools — for any key that does not hold all four. That is the only way an
  agent learns the surface exists, so it is worth reading on the first call.

Grant tiers when minting: `tool_tiers: ["prd"]` on `POST /api-keys`, or the tier buttons under
Settings → API keys. `fleet.mint` gives a planner credential the `fleet` tier automatically. An
operator can restore the pre-tiering manifest for a whole deployment with
`MCP_DEFAULT_TOOL_TIERS=prd,codegraph,fleet,misc` — see [configuration.md](configuration.md).

## The 56 tools

The manifest you receive is gated twice. **By scope** (AL-78): a key without `write` is not
shipped the mutating tools it would only be refused on. **By role** (PRD-17 D-b): a key whose
`roles` name a single role is not shipped the other roles' tools — a reviewer credential
carries no `claim_next`, a worker credential no `sign_off`. A single-role fleet key sees
roughly 16–19% fewer tokens.

**`grill_prd` generates questions and advances nothing** (GRPH-513). There are two grill
modes. `grill_prd` is a drafting aid: it calls the model, returns a markdown list, and writes
no turn, no dimension and no status change. `answer_grill` is the loop that records, grades and
advances — and approval is *earned* by finishing it.

The description used to end "answer via `update_prd`". True about how to record an answer, and
silent about what recording it that way achieves — which is nothing, because `update_prd`
replaces a body and touches neither `grill_turns` nor `grill_dimensions`, and those are exactly
what the status is read from. Four rounds were run that way against a PRD; it ended with 25,293
characters of worked-through document, **0 turns, 0 dimensions, status `draft`**. Nothing was
broken and nothing said so.

So the payload now carries `records_answers: false`, `turns_recorded` and
`dimensions_outstanding` beside the questions. A credential can hold `grill_prd` without
`answer_grill` — the manifest is trimmed by scope — so an agent may have no other way to find
out that its answers are not counting.

**Every tool argues its role gate** (GRPH-516). 15 of the 56 tools are role-gated; the other
40 are callable by every role. That was a *default* rather than a decision — `TOOLS` has had a
completeness guard forcing each new tool to be classified as a quality gate or an authority
one, and `TOOL_ROLES` had no equivalent, so forty tools arrived at "open to everyone" without
anyone arguing for one of them. Some are certainly right; nobody could tell which from the
file.

Every tool now appears in exactly one of `TOOL_ROLES` (gated, with the role) or `OPEN_TOOLS`
(open, with the reason). Adding a tool to either map is forced — the suite goes red otherwise.

**Nothing new was gated.** `heartbeat` is the warning: it *was* gated, that was the bug, and it
took reviewers and planners off the roster 150 seconds after they registered. Four more gates
today would be four more chances to repeat that. The guard makes the next forty arrive already
argued.

Seven entries are marked `NOT ARGUED` — the candidates worth deciding, recorded as debt rather
than given an invented rationale, because a fabricated argument reads exactly like a considered
one. A test pins that count so the list can shrink and not grow.

Two entries are open *load-bearingly*, and say so: `register_agent` (a caller cannot hold a
role before it registers, so gating it deadlocks) and `heartbeat` (it extends agent presence,
not only an item lease).

**A guessed project says so** (GRPH-482). A call resolves its project as
`project_id` → the key's own default → *whichever project sorts first*. That last step is an
ordering, and on a key spanning several projects a call that names none used to land in one
of them silently.

When it happens, the response now carries `resolved_project` and a note saying to pass
`project_id`. Only then — a key scoped to one project has nothing to choose between, and a
key with its own default had that chosen by whoever minted it. A note on every response is
how a field that matters gets skimmed past.

**The call is not refused**, and that is deliberate. Refusing would break every existing
multi-project caller, including agent prompts already in the wild that cannot be edited from
the server. The frontend hit this same class of bug and fixed it by making the project
explicit at the call site rather than rejecting the call.

**A lean page says what its rows carry** (GRPH-440). `search_items` and `get_backlog` return
a compact row — `id`, `title`, `status` — and the full item only on `fields=full`. That
projection is deliberate; these reads return many rows. What was not deliberate is that it
looked complete: a consumer asking a row for `built_by` got nothing, and in every client
language absent arrives as null. "Nobody built this" and "this payload does not say" were the
same answer, on the exact field a reviewer consults to decide what it may take — misread twice
in one day from two different tools.

So a lean response carries a `fields` array naming what each row holds. **A field absent from
that list is unreported, not empty.** `fields=full` omits the array entirely, because it omits
nothing — and because a list there would invite a caller to treat it as the item's whole
vocabulary, which it is not (`intent_hold` appears only on the rows that have one).

**A section can declare what it is** (GRPH-247). `prd_coverage` decides whether a `## `
section is buildable work or framing prose, and it used to decide purely from the heading
name against an allowlist. That allowlist cannot keep up with headings people invent: three
sections on this repo's own approved PRD-17 — `Key decisions`, `Roles`, `Relationship to
in-session orchestrators` — were pure framing, missing from it, and therefore reported as
coverage gaps that could never close. Two already carried hand-retitled "Spec:" items created
only to silence the report.

Put `<!-- framing -->` anywhere in a section's body and it is framing. Put
`<!-- buildable -->` and it is work. Either beats the name in both directions — which matters,
because `Decisions from grilling` is on the allowlist while a plain `Decisions` deliberately
is not, on the grounds that it may be design decisions that do need building.

Every section in a coverage report now carries `implementable_basis` naming the rule that
decided: `marked <!-- framing -->`, `a conventional framing heading`, or `no framing marker
and not a conventional framing heading`. A false gap is found by somebody disbelieving a
report, and the next question is always why.

**What was deliberately not done:** inverting the default, so a section with no acceptance
markers counts as framing unless it says otherwise. Measured against the PRDs in `docs/`, that
reclassifies 30 sections and most are genuinely buildable — it trades a visible false gap for
an invisible missing one, and work that quietly stops being counted is worse than a gap nobody
can close.

**Coverage reports whether items still AGREE with their sections** (GRPH-360). `decompose_prd`
copies a section's markdown into the item it creates, and that copy is never refreshed — so a
PRD edited afterwards leaves its items holding the old rules. Found live on PRD-17, where nine
of eleven items had drifted from the approved body: one specified a `403` where the PRD
specifies an `unauthorized` tool error, and one carried nine acceptance steps against the
PRD's twelve, missing the attack the grill was run to find. `prd_coverage` read **100%
covered** throughout, because it matches items to sections by NAME — it measures existence,
not agreement.

Each item now records a fingerprint of the section it came from, and `prd_coverage` reports
one of five states per item, plus a `drift_counts` rollup:

| state | meaning |
|---|---|
| **agrees** | the section is unchanged since this item was created |
| **drifted** | the section has been edited; the item's description is the OLD text |
| **acknowledged** | a human reviewed this divergence and kept it |
| **section_gone** | the section was renamed or deleted — the item is orphaned |
| **unknown** | no fingerprint: created before this existed, or linked by hand |

**`unknown` is an answer, not a fallback.** An item that cannot be checked and one that has
been checked and agrees are different facts, and collapsing them is how the PRD-17 drift stayed
invisible. Orphaned items are reported in their own `orphaned` list, because coverage iterates
the PRD's own headings and a renamed-away item is in none of them.

**Nothing is ever rewritten.** An item is legitimately edited away from its section — narrowed
after a spike, annotated with what the build found — so this detects and a human decides. To
keep a divergence, call `update_item(id, ack_section_drift=true)`; that acknowledges the text
as it stands now, so a LATER edit flags again.

**Editing a PRD does not mean replacing it** (GRPH-357). `update_prd` used to take one
thing: the entire markdown body. An agent asked to record a single decision had to reproduce
the whole document from memory, and anything it failed to reproduce was gone — silently, with
no snapshot written on the MCP path to recover from. `get_prd` made the read possible; a
sentence in the tool description telling you to use it is not a guard.

So there are two forms now, and the safe one is the cheap one:

- **`section`** — `update_prd(prd_id, section="3. Resolution order", body="…")` replaces that
  heading's contents. Every other byte is spliced back verbatim. It needs no read token,
  because it cannot lose what it never read. Section titles match loosely enough that
  `Resolution order` finds `## 3. Resolution order`, and a title the PRD carries twice is
  refused rather than guessed.
- **`base_hash`** — a whole-body replace must carry the `body_hash` that `get_prd` returned,
  and is refused if the document has moved since. A hash rather than `version`, because
  `version` only advances on an explicit snapshot and so cannot tell you whether the body
  changed. The same mechanism `gbagent` uses for files.

This is enforced at the MCP boundary, not in the service: the REST caller is a human editing
a textarea they are looking at, and demanding a token from them would break the UI for no
safety gain.

**Annotations say only what differs from the spec default** (GRPH-48). MCP defines defaults
for all four `ToolAnnotations` hints — `readOnlyHint` false, `destructiveHint` true,
`idempotentHint` false, `openWorldHint` true — and an absent field means exactly that value.
Restating one costs bytes and tells a client nothing, so the manifest omits it. Read a hint
the way the spec does, filling in defaults for what was not sent: `create_item` advertises
that it is not read-only by saying nothing, and `update_item` advertises that it is
destructive the same way.

That is the only trim of its kind taken. The spec further says `destructiveHint` and
`idempotentHint` are meaningful only when `readOnlyHint == false`, which would drop both from
every read-only tool — worth another ~238 tokens, and lossless only for a client that honours
the conditional. A client reading the field regardless would flip to the opposite value, so
the saving is declined.

**What cannot be done, so nobody spends a week on it.** There is no server-side progressive
disclosure. `inputSchema` is a required field of every tool in `tools/list`; the spec has no
detail level and no on-demand schema, and it explicitly makes progressive discovery the
*client's* job. Pagination does not reduce total tokens, and `ttlMs`/`cacheScope` save round
trips rather than context. Scope and role gating are the real levers, and they are above.

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
| `get_context` | — | Orient: the key's project, scopes, project/tool counts, and **gitops** (base branch, push-to-base, naming, reviewer bar). Unset gitops fields are **unmeasured** — not `main` and not "no requirements". `gitops.control` is present when the box is linked (`linked_set` / `linked_unset` / `linked_unreachable`). Call this first. |
| `list_projects` | — | All projects (`id`, `name`, `tag`, `accent`, `description`) — ids for the `project_id` override; `tag` is the short prefix its item/request/PRD keys render with |
| `create_project` | `name`, `tag`, `description` | Create a project so `setup_project` has somewhere to bootstrap. **Self-host only, and only while unlinked** — refused in hosted mode and once the instance is linked to a cloud org, since a project created here would reach that org's tenant (AL-284) |
| `setup_project` | `project_id` | **First-run bootstrap** — an ordered, resumable checklist (confirm project → build graph → load memories → propose items). Read-only; call it when `get_context` reports an empty project |
| `next_cluster` | `agent_id`, `max_items`, `project_id` | **Claim a code-neighborhood at once** — the best ready item plus its related ready items, all assigned to you. |
| `related_work` | `id` | Items related to a task by shared touchpoints + typed links, best-first (read-only) |
| `claim_next` | `agent_id`, `lease_seconds`, `wait_seconds`, `project_id` | **Atomically** claim the best ready item, assign it to you, move it to in_progress. Returns `{claimed, item}`. |
| `propose_allocation` | `project_id` | What the fleet should look like given who is online and what is ready. A proposal — nothing is assigned until `assign_role` |
| `assign_role` | `agent_id`, `role`, `reason` | Commit a role change; it reaches the agent on its next poll as a `directive`, with no reconnect |
| `mint_enrolment` | `agent_id`, `role`, `wave` | PLANNER ONLY. Mint a seat for an agent you are spawning, bounded by your credential — the code is returned once and grants that role for one session |
| `retire_wave` | `agent_id`, `wave` | PLANNER ONLY. Revoke the seats YOU minted and release what agents on them hold, in one step — it does NOT stop processes, and `agents_still_running` names the ones still building against dead seats |
| `collision_clusters` | `project_id`, `status` | Partition ready work into clusters that provably share no touch-areas; `predicted` marks lower-confidence grouping |
| `claim_cluster` | `agent_id`, `max_items`, `lease_seconds`, `wait_seconds` | Claim a whole non-colliding cluster and reserve its areas, checked against in-flight work |
| `claim_review` | `agent_id`, `project_id`, `wait_seconds` | Lease an item in review you did **not** build, and are independent of — not your own call tree, and not the same credential on the same host |
| `sign_off` | `id`, `agent_id`, `evidence`, `commit` | Take a reviewed item to `done`. Refused if you built it — and, above effort 3, refused without a `sabotage` receipt. With `commit`, mints an `attestation` |
| `bounce` | `id`, `agent_id`, `reason` | Send it back to `next` with a reason, reserved for its author for one lease period |
| `register_agent` | `label`, `capabilities`, `worktree`, `branch`, `role_hint`, `parent_agent_id` | Register THIS process as an agent and learn its role. Two terminals on one key become two agents. Returns `{agent_id, key, active_role, enrolled, tools_off_limits, heartbeat_interval_seconds}` — `tools_off_limits` names the tools this role will be refused, which the manifest cannot, having been fetched before the role existed |
| `fleet_status` | `project_id` | Who else is working this project: agents, roles, derived presence, and what each holds |
| `heartbeat` | `id`, `agent_id`, `status`, `files` | Extend the lease on an item you hold **and** your agent presence (so neither is reclaimed while you work). `status` (one line) and `files` (paths you are editing) are what the Live page shows as *reported*; written to the feed only when they change (PRD-34) |
| `release_item` | `id`, `agent_id`, `to_status` | Return a claimed item to the queue |
| `create_item` | `title`, `description`, `tags`, `touchpoints`, `effort`, `status`, `fidelity`, `project_id` | Create a tracker item (returns its `project_id`) |
| `update_item` | `id`, `status`, `title`, `description`, `tags`, `touchpoints`, `effort`, `blocker`, `fidelity`, `prd_id`, `prd_section` | Patch / advance an item. `touchpoints` **unions** (like evidence appends); an empty list is not a write |
| `search_items` | `query`, `tags`, `status`, `fields`, `project_id` | Query the stream (query matches title, description, **and** tags); lean rows by default, `fields="full"` for all. Typed human waits are `status=blocked` plus a `wait:merge` / `decision` / `secret` / `access` / `deploy` tag — free-text `blocker` is not a wait |
| `add_memory` | `text`, `scope`, `item_id`, `project_id` | Record a memory shard. Resolved `status` follows the project's memory write mode: `review` → **`candidate`** pending human publish (AL-49, the default), `auto` → published only when strongly corroborated, `trusted` → published on write so an agent can read its own writes back (AL-280) |
| `publish_memory` | `shard_id` | **Submit** a candidate for independent adjudication — the judge decides, not the caller. Returns `{shard, verdict}`; `kept: false` is a normal outcome. Needs `agent_adjudication` on the project **and** a real chat model, else `unavailable` and the shard is untouched (AL-282) |
| `reject_memory` | `shard_id`, `reason` | Discard your own candidate. No judge needed — it removes nothing from the trusted pool (AL-282) |
| `search_memory` | `query`, `top_k`, `include_candidates`, `project_id` | Semantic search over **published** shards (set `include_candidates` for unreviewed ones); returns `status`, `item_id`, `source` |
| `get_lessons` | `shard_id`, `trend`, `caught_state`, `eligibility`, `lesson_class`, `limit`, `offset`, `project_id` | The published lesson catalog with computed effectiveness, caught-issues, and org-eligibility. `score` is null when unmeasured — not a high score. `eligibility` is unverifiable until users/projects are attributed and the published cluster is scanned. Pass `shard_id` for provenance, outcomes, and history |
| `get_backlog` | `limit`, `fields`, `project_id` | Prioritized backlog (lean rows + ranking fields by default, `fields="full"` for all) |
| `get_item_details` | `id` | Item + linked shards + linked requests |
| `suggest_next` | `project_id` | Best next item from state + memory |
| `link_items` | `a`, `b`, `type`, `reason` | Create a typed relationship |
| `unlink_items` | `a`, `b`, `type` | Remove a typed relationship (inverse of `link_items`); omit `type` to remove all types for the pair. Idempotent — returns `removed` |
| `extract_lessons` | `id` | Distil lessons into memory (returns `scheduled: true` immediately; shards land on `linked_shards`) |
| `generate_digest` | `project_id` | Compose a progress digest across the project |
| `prd_coverage` | `prd_id` | Spec-to-task rollup: per-section counts, `gaps` (no tasks), `empty_sections` (no substance). `shaped: false` is not a clean pass (read-only) |
| `decompose_prd` | `prd_id`, `create` | Propose (or create) one task per un-covered PRD section, each carrying the PRD's framing prose. `create=true` is refused unless the PRD is `approved` (grill earns it) |
| `create_prd` | `title`, `body`, `template`, `project_id` | **Author a PRD** (the handoff artifact) — `## ` sections drive decompose/coverage |
| `get_prd` | `prd_id` | The full PRD including its markdown `body` and a `body_hash` (GRPH-519) |
| `update_prd` | `prd_id`, `title`, `status`, `section`, `base_hash`, `body` | Patch a PRD. **`section`** replaces one `## ` heading's contents and leaves every other byte alone; a whole-body replace needs `base_hash` from `get_prd` (GRPH-357). **`approved` is not settable** — it is reached by finishing the grill; setting it returns `conflict` naming what is still outstanding (AL-300) |
| `answer_grill` | `prd_id`, `answer` | Relay the author's answer to a grill question — recorded as **agent-relayed**, visible to whoever reviews later. Returns `outstanding` + `complete` (AL-299) |
| `grill_prd` | `prd_id` | **Grill** — next clarifying questions to sharpen a PRD before building (read-only) |
| `describe_code` | `nodes`, `edges`, `prune`, `project_id` | **Record code structure** — upsert code nodes and typed edges. Idempotent by path; re-describe on change. `kind` is **`module`** = a package/directory, **`file`** = one source file, **`symbol`** = `path::name` inside a file, **`doc`** = prose (`AGENTS.md`, `README.md`), **`config`** = a file that configures rather than executes (`docker-compose.yml`, `nginx.conf`, `pyproject.toml`). Describe your docs and config too — work that touches them has a blast radius, and until GRPH-381 the graph could not represent them at all. A kind contradicting its path is stored as the path implies and reported in `kind_corrections` |
| `get_code_map` | `kind`, `project_id` | The project's code graph — described nodes + typed edges (read-only) |
| `code_neighbors` | `path`, `project_id` | Edges around a path (in/out by type) + work items touching it (read-only) |
| `graph_query` | `query` (`hubs`/`components`/`path`), `a`, `b`, `edge_types`, `limit`, `project_id` | **Structural questions** — inbound-degree hubs (what breaks if this changes), connected components each with an anchor, and the shortest path between two paths. Traversed undirected, reported with edge direction. Read-only and deterministic |
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

**Authority.** A key is bounded by its declared `scopes` **and** its
owner's project memberships — a key can never out-rank the user who minted it. A
project-scoped key is further pinned to that project; the `project_id` argument
selects among in-scope projects but cannot escape the scope. Call `get_context`
first: it reports `readable_projects` and `writable_projects` for the key.

| Scope | Grants |
| --- | --- |
| `read` | Read tools. Implicit, but accepted so a key can be minted read-only |
| `write` | Every mutating tool |
| `sync` | Push a code graph (`authz.key_sync_ids`); must be pinned to one project |
| `gate` | Write an `attestation` receipt — the proof a completion gate reads |

`gate` is deliberately **not** implied by `write`. An agent that could mint its own
attestation could certify its own work, which is the whole reason the scope is separate:
a building agent records `test` and `sabotage` receipts, and an adapter — CI, or a
reviewer signing off — attests the result. Keys without it are refused with
`unauthorized` and a hint naming what to do instead, and the attestation-only fields are
left out of their tool manifest entirely.

**Evidence kinds.** `test`, `url`, `screenshot`, `health`, `note` are advisory and fall
back to `note`. Two are structured, and a receipt that claims one without the required
fields is **demoted to `note`, never dropped** — the claim stays readable even when it
doesn't validate:

| Kind | Requires | Notes |
| --- | --- | --- |
| `sabotage` | `claim`, `mutation`, `tests_failed` | `tests_failed: 0` means the test *cannot* fail — a finding, not a pass |
| `attestation` | `adapter`, `commit`, `predicates` | Needs the `gate` scope. `predicates` is `[{name, passed, detail}]` — at least one, all `passed`, and `passed` must be a real boolean |

An attestation binds to the commit it names, and that binding is now read. `head_commit`
records what an adapter last **observed** — written whether its run passed or failed — and
completion requires an attestation for that commit. Pushing after attesting invalidates the
proof.

The failure-path write is the load-bearing half. A passing run writes a receipt for the new
head, so staleness never arises there; the dangerous sequence is attest at A, push B, CI
fails on B, no receipt written — and unless the head still moves, A's receipt goes on
vouching for code that has since broken.

`head_commit` needs the `gate` scope for the same reason the receipt does: an agent able to
set it to the commit its stale attestation names would walk straight through the check.

**With no head reported, the weaker check applies** — any valid attestation completes.
Refusing instead would make completion impossible for every install without such an adapter,
including the offline one, and `fleet.sign_off` reports no head. The stronger guarantee is
opt-in, and visibly so: an item completed under the weak check is exactly one whose
`head_commit` is empty.

**Completion is gated.** `update_item` refuses `status: "done"` unless the item carries a
valid attestation, and answers `conflict` — not `unauthorized`, because the caller *may*
complete it; the work simply is not accounted for yet. Over REST the same refusal is a
`409`. Every other transition is ungated: claiming, reviewing, blocking and releasing are
reversible, and gating the states agents pass through constantly is how a gate teaches
people to route around it.

The two halves are what make it hold. An agent cannot write the proof (no `gate` scope) and
cannot complete without it — so finishing work means getting it attested by CI, or by a
reviewer through `sign_off` with a `commit`.

Two adapters exist. **`fleet.sign_off`** needs a reviewer but no external service, which is
what keeps completion reachable offline. **CI** (`scripts/attest_ci.py`, run as a step in
the `ci` gate job) attests every item a green run's PR names, and needs no reviewer — set
`GRAPHBAN_URL` as a repository variable and `GRAPHBAN_GATE_KEY` as a secret to enable it.
Without them it skips loudly rather than failing the build, and items must be signed off
by hand. Fleet keys minted for the **reviewer** or
**all-in-one** role carry `gate` for that reason; a worker or planner key does not, because
a worker's ceiling is `review` by design.

Items that were already `done` are untouched — the gate asks about the *transition*, not
the state, so history is left alone.

**Refusals become memory.** A refusal is recorded twice: as a `note` receipt on the item,
and as a **published** memory shard, at refusal time rather than on any later completion —
a refused item never completes, so extraction-on-done would never fire for exactly the
cases worth learning from. `search_memory` surfaces it to whoever plans similar work next,
which is what turns enforcement into something that compounds instead of a wall people
learn to route around.

Published rather than filed as a candidate, deliberately: the trusted-publication boundary
keeps *unreviewed agent self-reports* out of retrieval, and a gate refusal is not one — the
server refused and knows why. `origin: "gate"` keeps them filterable. One shard per
`(item, predicate)`, updated in place when the reason changes, so a repeated refusal cannot
flood the corpus it exists to improve.

> **Known asymmetry.** The `gate` scope is enforced on the MCP boundary. The REST path
> authenticates a human with write access to the project — the same authority that mints
> gate keys — and JWT sessions carry no scopes, so a signed-in user may attest through
> `PATCH /api/items/{id}`. That is deliberate for now rather than decided; it is recorded
> here so it is not mistaken for an oversight.

### Spec → task traceability

Items can link to a **PRD + section** (`prd_id` / `prd_section` on `create_item`/`update_item`),
so the spec and the tracker stay joined. `decompose_prd(prd_id, create=true)` proposes one
tracked task per un-covered PRD section (the gaps) and, with `create`, creates them as backlog
items linked back to the section — the spec drives the tracker. Filing is refused until the
PRD is `approved` (the grill earns it); a dry-run still proposes on a draft.

**Each task carries the PRD's framing sections with it** (GRPH-261). A section body reads
correctly inside a document and incompletely outside one: the rules an implementer needs —
an invariant, a charset, a set of assigned values — live in Overview, Context or Goals, and
the buildable sections assume the reader has already seen them. On PRD-13 the tag charset and
the five tag assignments never reached the six items meant to implement them, and all six had
to be rewritten by hand. The framing is duplicated onto every item on purpose: an item that
must fetch its parent to be actionable is the cost this tool exists to remove.

The block is **bounded and says what it left out** (GRPH-428). Framing across this repo's
PRDs runs 7,461–15,819 characters, and copying all of it onto every task put ~3,300 tokens
of duplicated prose on each — against an MCP manifest of ~13,200 whose ceiling has been
argued five separate times. The budget is 8,000 characters, sections are taken in document
order because PRDs state their rules first, and anything that does not fit is **named** in a
`Not carried` block rather than silently dropped. A short block and a PRD with nothing more
to say must not look the same.

The copy also carries the PRD's **version**, because re-decompose skips sections that
already have an item and therefore never refreshes it. `intent_hold` already warns that
intent has moved; the stamp is what tells a reader the rules in front of them are the old
ones rather than merely that scope changed.

Relative references (`the five tags above`, `the mint path below`) are **reported, not
rewritten** — they arrive on each proposal as `dangling_refs`, visible on a dry run before
anything is created. Repairing them means guessing which five, and a confidently wrong
substitution reads as fact where a visible "above" tells the reader to go looking. `prd_coverage(prd_id)` returns
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
`web/src/lib/api.ts`, a symbol name) — on `create_item`/`update_item`. `update_item`
**unions** the list (P30 D10): the client sends this reap's measured paths only, and an
empty list is not a write — wiping declared paths would read as "no collision". Two items
relate when their touchpoints overlap (exact, glob, or same directory), and sharing a
touchpoint **auto-creates a `code` link** between them. Then:

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

  **`content_hash` has a stated rule** (GRPH-406): `sha256:<hex>` of the file's contents
  with trailing whitespace stripped, UTF-8 with errors replaced. Trailing whitespace is
  normalised because an editor adding a final newline is not a change, and a staleness flag
  that fires on that is one people learn to ignore.

  The rule exists because a hash that only ever meets its own previous value needs no
  rule, and two places need more than that. `code_sync` decides what an incremental push
  sends by comparing hashes, so a second agent hashing differently re-pushes every path it
  touched — and PRD-17 made several agents describing one project normal. Cross-repo
  duplication detection has no other proof-grade signal at all, since the cloud holds
  summaries and structure but never source.

  Hashes written before the rule are **unprefixed and stay valid**. Nothing is rejected and
  no migration runs: 498 of the live graph's nodes carry a bare hash today, and refusing
  them would make a re-describe fail against the real graph rather than improve it.

  What the rule buys is enforced at the one point the server controls. **A hash whose rule
  is unknown never overwrites one whose rule is known**, so provenance can improve and never
  degrade — and `describe_code` reports the paths it kept in `hash_retained`, the same
  contract `kind_corrections` uses. An agent that learns its hashes are being refused can
  adopt the rule; one that is never told cannot.

  That is what removes the churn. Two agents describing one project is normal since PRD-17,
  and before this an agent that had not adopted the spec destroyed the provenance one that
  had just established — after which `code_sync.compute_diff` re-pushed every path it
  touched. `compute_diff` itself is unchanged, deliberately: both sides of its comparison
  come from the same local database, so once the stored hash cannot degrade, the diff is
  consistent without consulting provenance itself.
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
`release_item(id)` hands it back — and releases a REVIEW claim too, if that is the hold you have. Both claim calls take `skip`: a list of ids you have already declined, because releasing an item does not move you past it (it is top-scored again on the next call).

Releasing an item you never wrote to also clears `built_by`. Claiming is how you SEE what the queue holds, so being recorded as the author of everything you declined would bar you from later reviewing it.

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
