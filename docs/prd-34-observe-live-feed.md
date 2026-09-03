# PRD-34 — Observe Live feed: what an agent is doing

**Ledger id:** GRPH-P34
**Status:** approved — grill complete 2026-09-03; answers absorbed into D15–D22 and §9. Successor to PRD-33 §8 ("Not this PRD"). v1.0.
**Depends on:** PRD-33 (the Live board, `services/live.py`, `/live`) · PRD-17 (Agent, presence, `touch`) · PRD-19 (session → agent, `mcp_session_id`) · AL-43 (the audit ledger, `Event`)
**Complemented by:** Activity (audit ledger) · Fleet view (provisioning) · Code graph presence (spatial)
**Touches:** `backend/app/mcp_server.py` (dispatcher audit point, `heartbeat` handler) · `backend/app/services/live.py` · `backend/app/services/fleet.py` (`touch`) · `backend/app/services/agent_calls.py` (new) · `backend/app/routers/live.py` · `backend/app/models/__init__.py` (`AgentCall` new, `Agent` +3 columns) · `backend/alembic/versions/0102_*` · `web/src/features/live/LiveView.tsx` · `web/src/lib/api.ts` · `web/src/lib/queries.ts` · `web/src/lib/types.ts` · `AGENTS.md` · `.cursor/rules/agentledger.mdc` · `fleet/src/gbagent/coord.py`

---

## 1. Overview

<!-- framing -->

PRD-33 shipped a board that answers **who is here**: humans → agents → holdings → leased files → recorded PRs. Read against a working fleet it is a roster. The operator's next question — **what is that agent doing right now, and what did it do in the last ten minutes** — has no answer on the page, because Graphban does not store it.

This PRD adds the **feed**: a per-agent timeline under each Live row, built from two sources that are stored separately and named separately on every row.

1. **Observed.** Every accepted MCP call, reads included, attributed to the agent that made it. Graphban is the server the agent calls; the dispatcher already sees every call and already resolves the agent for the manifest. It just does not write reads down. The feed is what the agent asked Graphban for: "searched code for `reservation lease`", "read GRPH-691", "claimed a cluster", "moved GRPH-700 to review".
2. **Reported.** `heartbeat` gains an optional one-line `status` and a list of `files` the agent says it is editing. Self-reported, like `worktree` and `branch` already are, and rendered as such with its age.

The load-bearing invariant, inherited from PRD-33 and extended:

**Every feed row names its source. Observed and reported never share a wording. Silence is a named state with an age, never an empty list.**

An agent with no calls in twelve minutes shows "no calls for 12m", not a blank. An agent that never reported a status shows "no status reported", not a missing line. A status older than the presence TTL is labelled stale, not shown as current.

### 1.1 What this is not

- **Not observed file writes.** Hooks that post editor activity (Claude Code `PostToolUse`, Cursor hooks) are the agenttrail insight PRD-33 deferred, and they stay deferred. That is a new ingest path with retention, privacy and credential questions of its own. This PRD builds the timeline those writes would land in.
- **Not Activity.** Activity is the audit ledger (AL-43): one row per accepted **mutation**, attributed to the credential, kept forever. The feed is telemetry: every call, attributed to the **agent**, kept for days. They share the dispatcher and nothing else. Growing `events` ten-fold with reads would change what Activity is.
- **Not a push channel.** PRD-33 D7 stands: poll on the fleet clock. No SSE, no websocket, in this PRD.
- **Not a summariser.** No model reads the feed and writes "the agent is fixing the tests". The row says what the call was. A later PRD may add that on top; it must not replace the rows.

---

## 2. Problem

<!-- framing -->

Verified against the tree at `8ee6256d`, 2026-09-03, after reading the merged PRD-33 slices (PRs #567, #576, #592) on the deployed instance.

### 2.1 The dispatcher audits mutations only

`mcp_server.py` calls `_audit_tool` after a successful dispatch **unless** the tool is in `_READ_ONLY`. That set is 21 tools and is most of what a working agent calls: `get_context`, `get_item_details`, `search_code`, `code_neighbors`, `get_code_map`, `graph_query`, `search_memory`, `get_lessons`, `related_work`, `fleet_status`, `get_prd`, `prd_coverage` … None leave a trace. `mcp_stats.increment` runs for every call but is a per-tool counter with no actor and no time.

So between `claim_next` and `update_item(status="review")` — the whole of the work — the server records nothing, and a stuck agent and a busy agent look identical.

### 2.2 What is audited is attributed to the key, not the agent

`events.record_key` stamps `actor_type="apikey"`, `actor_id=key.id`. The agent id reaches `meta` only on two refusal paths (`sign_off_refused`, `role_refused`). A fleet of four agents on one credential — the shape PRD-19 explicitly supports — is one actor in Activity. The Live board groups by agent; the ledger cannot be joined to it.

### 2.3 Heartbeat carries nothing

`heartbeat` takes `id` and `agent_id`. The handler calls `fleet.touch(db, agent, state=...)`, which sets `last_seen_at` and `state`, and `items.heartbeat` to extend the lease. There is no field for what the agent is doing. The operating loop in `AGENTS.md` step 2 says "heartbeat while you work"; it has nowhere to put "what".

### 2.4 Failed calls vanish

A refused call returns `_tool_error` before the audit point. A quarantined or role-refused agent that is retrying the same call every thirty seconds is exactly what "stuck" looks like, and it is invisible except as `state: quarantined` on the roster.

### 2.5 The board is honest and reads as idle

PRD-33 §9 Risk 1 predicted the bug report: "every row is `unreserved` / `unrecorded`". It arrived on the first day. The fix PRD-33 named — keep the words — holds. The fix this PRD adds is the missing measurement.

---

## 3. Goals

<!-- framing -->

1. A **feed** under each agent row on `/live`: newest first, each row `{at, source, tool, target, ok}`, with the row's source visible.
2. **Observed rows for every accepted call, reads included**, attributed to the agent, written by the dispatcher in one place. Failed calls are rows too, with their error code.
3. **Reported status** on `heartbeat`: `status` (one line) and `files` (paths). Stored on the agent, echoed on the board with its age, and written to the feed **on change** so the status history is in the same timeline.
4. **Silence is stated.** `no_calls` / `quiet` with seconds; `unreported` / `stale` for status. An empty feed array without a state is invalid, the same rule as PRD-33 D6 for `files: []`.
5. **The feed table is not the audit ledger.** New table, bounded retention, deleted by age. `events` is untouched in shape; it gains only `meta.agent_id` so Activity can name the agent too.
6. **JWT-only**, `require_readable`, same posture as `GET /live`. The feed is a map of a person's agents onto queries and files. Not on MCP.
7. **Producers ship with the consumer.** The PR that adds `status` to heartbeat also updates the operating loop (`AGENTS.md`, the Cursor rule, `gbagent`'s timer heartbeat, the Fleet mint prompt) so a real fleet reports on day one. A field nobody is told to fill is `unreported` forever.
8. First PR leaves `main` deployable. A feed that is all `get_context` / `search_code` rows is the truth about a `claim_next` agent and is acceptable. A feed that hides reads to look purposeful is not.

---

## 4. Non-Goals

<!-- framing -->

- Observed file writes from editor hooks. Successor PRD. This PRD defines the row shape (`source`) they will use and nothing else.
- SSE, websockets, or a poll faster than `heartbeat_interval_seconds`.
- Storing call **arguments** or **results**. One extracted `target` string per row, from a per-tool allowlist, truncated. No JSON blobs of what an agent searched for or what it got back.
- Summarising the feed with a model.
- Changing what Activity lists, its retention, or its attribution model beyond adding `meta.agent_id`.
- An MCP tool to read the feed (`get_live_feed`). Presence is JWT-only (PRD-20, PRD-33 D5); the feed is more of the person, not less.
- Turning reported `files` into reservations, predicted or otherwise. A report is a claim; a lease is a write to `AreaReservation` by `claim_cluster`. Reported files are a fourth labelled kind on the files list (D7) and do not move `file_state`.
- Guessing the agent for an unattributable call. A call with no `agent_id` and no unique live agent on its session is written with `agent_id = NULL` and counted, not assigned.
- A "current tool" run card, a canvas, or session trails (agenttrail's product).
- Feed rows for REST calls from the UI. The feed is what **agents** do; a human clicking in the tracker is Activity's row, already.

---

## 5. Key decisions

<!-- framing -->

These are closed. Implementation of a slice must not quietly reverse one.

| # | Decision | Consequence |
| --- | --- | --- |
| D1 | Two sources, named on every row: `source: "observed" \| "reported"`. | No merged "activity" wording. The UI renders them with different marks. A row without `source` is invalid. |
| D2 | Observed rows go to a **new table `agent_calls`**, not `events`. Written by the dispatcher at the one point every call passes through, for every tool, success or failure. | Migration `0102`. `events` keeps AL-43 semantics. `_READ_ONLY` still gates the audit ledger; it does not gate the feed. |
| D3 | Attribution order: explicit `agent_id` arg → the unique live agent on `mcp-session-id` (the manifest-trimming rule, reused) → `NULL`. `api_key_id` is always set. | No guessing. Unattributable calls are a counted third state on the board, per credential. `_audit_tool` gains `meta.agent_id` from the same resolution so Activity can say it too. |
| D4 | A row stores **one `target` string** from a per-tool extractor allowlist, plus `tool`, `ok`, `error_code`, `duration_ms`. No arguments, no results. | `search_code` → the query (≤120 chars). `get_item_details` → the item id. `update_item` → `id` + new `status`. Unknown tool → `target = ""`. The allowlist is a dict in `agent_calls.py`; adding a tool to the surface without adding it there yields an honest empty target, not a crash. |
| D5 | `heartbeat` gains `status: str` (≤200 chars, trimmed) and `files: list[str]` (≤20 paths). Stored on `Agent` as `status_text`, `status_files`, `status_at`. | Last-write-wins on the agent. Presence-only heartbeats with no `status` change nothing but `last_seen_at`. |
| D6 | A heartbeat that carries a status is written to the feed as `source: "reported"` **only when the status or files differ** from what the agent currently holds. Plain lease-extends are not rows. | Status history lives in the feed. A lease-extend every ~100s does not become 864 identical rows a day. Presence is already visible as last-seen. |
| D7 | Reported files render as kind `reported` on the Live files list, alongside `leased` / `predicted` / `off_map` / `declared`. They do **not** enter the D3/D16 priority table of PRD-33. | An agent editing files with no lease stays `unreserved`. Its row also lists what it says it is editing, labelled. Same rule as `declared` in PR 3 of PRD-33. |
| D8 | Board payload (`GET /live`) gains per agent: `last_call {tool, target, at, ok}`, `calls_in_window`, `silence_seconds`, `status {text, files, at, stale} \| null`, `status_state`. The **timeline** is a second read: `GET /live/{agent_id}/feed`. | The board stays one aggregation (PRD-33 D5) and does not grow a per-agent history join. Expanding a row fetches the feed. Both JWT, `require_readable`. |
| D9 | Poll on the fleet clock, same as PRD-33 D7. The open feed refetches on the board's interval. | No second cadence. No SSE. |
| D10 | Retention by age: `AGENT_CALL_RETENTION_DAYS` (default 7). Swept **on the write path, amortised** (every Nth insert), because `main.py` has no scheduler and this PRD does not add one. | Bounded table. A quiet instance sweeps rarely and has little to sweep. Sweep failure never fails the call. |
| D11 | Silence states are named. Calls: `never` (no rows in retention) · `quiet` (with `silence_seconds`). Status: `unreported` · `reported` · `stale` (older than `presence_ttl_seconds`). | An empty feed is `{state: "never"}`, not `[]`. A `reported` status shows its age. |
| D12 | Producers land with the field: `AGENTS.md` step 2, `.cursor/rules/agentledger.mdc`, `gbagent`'s timer heartbeat, and the Fleet view mint prompt all say what to put in `status`. One sentence each. | The feed has something to show on day one. The prompt does not grow a paragraph. |
| D13 | Failed calls are rows with `ok: false` and `error_code`. The dispatcher writes the row on the error path too, from the same helper. | A retry loop is visible as one. Refusals stop being the only audited failure. |
| D14 | Both engines. `agent_calls` is plain columns plus a JSON `files`; no vector, no Postgres-only type. Tests run on SQLite and Postgres where the write and the sweep hit SQL. | Same rule as every other PRD since GRPH-352. |
| D15 | No ghost rows. Calls with no resolvable agent get no per-agent feed. They are counted **per credential** and shown on the owning user group as "N calls on credential *key* not attributable to an agent". | An agent at `never` beside a non-zero group count is the signal. The board names the key, never guesses the agent. |
| D16 | Collapse (PR 3) is keyed on `(tool, target)`, never on `target` alone, and happens **client-side**. The tool name renders before the target on every row. | A search whose query looks like an item id still reads as a search. The table stays one row per call. |
| D17 | Change detection for reported status: `status_text` differs after trim, **or** `set(files)` differs. Order-insensitive. Compared against the `Agent` columns, not an in-process cache. | a → a+b → a is two rows. A server restart cannot forget the last status and duplicate it. |
| D18 | Sweep failure is counted, not swallowed: warning log + `agent_calls_sweep_failed` on `/health`. `POST /live/sweep` (admin) runs the same code path by hand. | No silent growth. No scheduler. The deployed box says when retention stopped working. |
| D19 | `agent_calls.summary` is **two statements per board**, whatever the agent count: one `GROUP BY agent_id` over the window, one latest-row-per-agent. | Never a per-agent loop. The feed endpoint's `limit` has nothing to do with `calls_in_window`. |
| D20 | A skipped producer degrades to `unreported`; `status` and `files` are optional so no loop breaks. CI asserts the clause is present in `AGENTS.md`, the generated Cursor rule, and `gbagent`'s timer heartbeat. | Acceptance 18. The rule-generation test catches a skipped regeneration. |
| D21 | Silence states are derived at query time from the latest row only. `never` = zero rows inside retention now; `quiet` → `never` the moment the sweep removes the last row; one new call → `active` immediately. | No lag, no stored state machine, nothing to drift. |
| D22 | Open questions closed (§9): retention 7 days; window 10 × heartbeat interval; `status` stays on `heartbeat` only; reported files render on Live only; collapse client-side. | Nothing in this PRD needs a prototype. |

---

## D1 — Two sources, named on every row

<!-- buildable -->

A feed row is:

```
{ id, at, source: "observed" | "reported",
  tool, target, ok, error_code?, duration_ms?,
  status?, files? }          # reported rows only
```

`source` is a column, not a derivation. The view renders observed rows with the tool name in mono and the target after it; reported rows with a "reported" mark, the status text, and the files list. No row says "activity". No row omits its source.

The wording rule from PRD-33 D6 carries: observed is what Graphban measured; reported is what the agent said. A reported row is never rendered in the observed style because it "looks nicer".

---

## D2 — The dispatcher writes every call

<!-- buildable -->

**Where.** `mcp_server.py`, the single dispatch point that today does `mcp_stats.increment` then `if name not in _READ_ONLY: _audit_tool(...)`. Add one call before the `_READ_ONLY` check, on the success path, and one on the `_tool_error` paths, both to:

```
agent_calls.record(db, *, project_id, api_key_id, agent_id, tool, target, ok, error_code=None, duration_ms=None)
```

`duration_ms` is measured around the `_dispatch` call. `project_id` comes from the resolved project the same way `_audit_tool` reads it from the result, falling back to the key's project resolution used for the call.

**Not `events`.** `Event` is append-only forever and is the audit ledger. `agent_calls` is telemetry with retention. A test asserts that a read-only tool call produces an `agent_calls` row and **no** `events` row (sabotage: route reads through `_audit_tool`).

**Never fail the call.** `agent_calls.record` swallows and logs like `events.record` does. A feed write error is not an agent's problem.

**Service.** `backend/app/services/agent_calls.py`: `record`, `feed(db, agent_id, *, limit)`, `summary(db, project_id, agent_ids, *, window_seconds)` returning per-agent `last_call`, `calls_in_window`, `silence_seconds`, `unattributed_calls` per key. `live.board` composes `summary`; it does not query `AgentCall` itself (PRD-33 A12 extended).

---

## D3 — Attribution, and the counted third state

<!-- buildable -->

Resolve the agent for a call, in order:

1. `args.agent_id` when present and on this credential (`fleet.caller_identity` already checks this shape; a foreign agent id is a refusal, already).
2. The unique non-offline, non-dismissed agent whose `mcp_session_id` matches the request's `mcp-session-id` header. This is the existing manifest-trimming rule; lift it into `fleet.agent_for_session(db, request) -> Agent | None` and call it from both places so they cannot drift.
3. `None`.

`api_key_id` is always set. A `NULL` agent row still counts. The board carries, per user group, `unattributed_calls` in the window, and the group's copy reads **"N calls on credential *key name* not attributable to an agent"** — on the group that owns the key, naming the key, because the ambiguity is a property of the credential and not of any one agent (D15). That number is the honest measure of how many callers on the project never passed `agent_id` — it is a prompt to fix the harness, not a bucket to hide.

**No ghost rows.** Unattributable calls get no per-agent feed and no synthetic agent. An agent row at `call_state: "never"` beside a non-zero count on its group is what "this credential is calling but not as you" looks like. The board must not pick the only live agent on the key and hand it those rows; on a shared credential that would put one agent's queries under another's name.

**Activity.** `_audit_tool` passes the same resolved id as `meta.agent_id`. `_principal_and_agent` prefers `meta.agent_id` over the key label when present. Activity's "via" then names the agent, not only the key. No schema change to `events`.

---

## D4 — One target string per tool

<!-- buildable -->

`agent_calls.TARGETS: dict[str, Callable[[dict, Any], str]]`. Examples:

| Tool | `target` |
| --- | --- |
| `get_context`, `get_item_details`, `heartbeat`, `release_item`, `claim_review`, `sign_off`, `bounce` | the item id |
| `update_item` | `"{id} → {status}"` when `status` is in args, else the id |
| `claim_next`, `next_cluster`, `claim_cluster` | the claimed id(s) from the **result**, joined, ≤120 chars |
| `search_code`, `search_items`, `search_memory`, `graph_query` | the query, ≤120 chars |
| `code_neighbors`, `get_code_map` | the path / node argument |
| `get_prd`, `prd_coverage`, `prd_acceptance`, `grill_prd`, `decompose_prd` | the PRD id |
| `add_memory` | the returned shard id, never the text |
| `register_agent`, `fleet_status`, `list_projects` | `""` |
| anything else | `""` |

Rules: the extractor receives `(args, result)` and returns a string; it never raises past `record` (wrap, log, `""`). Targets come from **arguments**, except the claim tools, whose target is the claimed id(s) from the **result**. Two tools sharing "the item id" logic is fine: the `tool` column disambiguates, every row renders the tool before the target, and PR 3's collapse is keyed on `(tool, target)` (D16) — a `search_code` query that happens to look like an item id never collapses into a `get_item_details` run. No extractor stores memory content, evidence bodies, PRD bodies, or a full argument dict. A test iterates every name in the tool manifest and asserts the extractor returns a `str` for an empty args/result without raising — the same "every tool has a row in the table" shape as the read-only set test.

---

## D5 — Heartbeat reports, on change

<!-- buildable -->

`heartbeat` schema gains:

```
status: { type: string, maxLength: 200, description: "One line: what you are doing right now." }
files:  { type: array, items: string, maxItems: 20, description: "Paths you are editing. A claim, not a lease." }
```

Handler: after `fleet.touch`, if `status` or `files` is present, `fleet.report_status(db, agent, status, files)`:

- trims, caps, writes `Agent.status_text`, `Agent.status_files`, `Agent.status_at = now`;
- returns whether anything **changed** versus the stored values — `status_text` differs after trim, **or** `set(files)` differs; order of `files` is irrelevant; the comparison is against the `Agent` columns, never an in-process cache, so a restart cannot forget the last status and write it again (D17);
- on change, the dispatcher's feed row for this call is written with `source: "reported"`, `status`, `files`. On no change, the feed row for the heartbeat is **not written** (D6) — the agent's presence already moved.

A heartbeat with neither field is presence-only and is not a feed row either. The feed is not a heartbeat log.

`status` on the credential path (no registered agent) is refused with the existing "call register_agent first" validation — there is no agent row to hold it.

**Producers (D12).**

- `AGENTS.md` operating loop step 2: "Heartbeat while you work, and say what you are doing: `heartbeat(id, agent_id, status='running the backend suite', files=['backend/tests/test_live.py'])`. One line. It is how the Live page knows what you are doing."
- `.cursor/rules/agentledger.mdc`: the same sentence (it is generated from `AGENTS.md`; regenerate, do not hand-edit).
- `fleet/src/gbagent/coord.py`: the timer heartbeat sends `status` from the loop's current phase (the loop already knows whether it is claiming, editing, testing, or handing to review) and `files` from the paths it last wrote.
- Fleet view mint prompt: the pasted prompt already says heartbeat; add the `status` clause.

---

## D6 — Board summary and the feed read

<!-- buildable -->

**Board.** `GET /live` gains per agent:

```
last_call: { tool, target, at, ok } | null,
calls_in_window: int,           # window = 10 × heartbeat_interval_seconds
silence_seconds: int | null,    # since last_call.at; null when never
call_state: "never" | "quiet" | "active",
status: { text, files, at, stale } | null,
status_state: "unreported" | "reported" | "stale"
```

and per user group `unattributed_calls`. `active` means at least one call inside one heartbeat interval; `quiet` means older than that; `never` means no row in retention. `stale` means `status.at` is older than `presence_ttl_seconds`.

**Two statements, not N (D19).** `agent_calls.summary(db, project_id, agent_ids, window_seconds)` issues one `GROUP BY agent_id` over `ts >= now - window` for `calls_in_window`, and one latest-row-per-agent query (max `ts` per agent joined back to the row) for `last_call`. A board of a hundred agents costs the same two statements as a board of one. A test counts statements with the engine's event hook and fails on a per-agent loop.

**Derived at query time (D21).** `call_state` comes from the latest row's age alone: `never` when no row exists inside retention at the moment of the query, `active` when the latest row is within one heartbeat interval, `quiet` otherwise. A quiet agent becomes `never` the instant the sweep deletes its last row; one new call makes it `active` immediately. Nothing is stored, so nothing lags.

**Feed.** `GET /live/{agent_id}/feed?project_id=&limit=` (default 50, max 200). JWT, `require_readable`. 404 for an agent not on the project. Returns `{ served_at, state: "never" | "ok", retention_days, rows: [...] }`. `state: "never"` with `rows: []` is the empty. `rows: []` with `state: "ok"` is invalid.

**View.** Each agent row shows one line under the holdings: the last call (or "no calls for 12m" / "no calls recorded") and the reported status with its age (or "no status reported"). Clicking the row expands the feed, fetched with `useLiveFeed(projectId, agentId)`, `refetchInterval` from the board's `heartbeat_interval_seconds`. Reported rows carry a "reported" mark; observed rows the tool in mono. Failed rows are tinted and show `error_code`.

---

## D7 — Silence, encoded

<!-- buildable -->

| Fact | Encoding | Forbidden shortcut |
| --- | --- | --- |
| Agent has no calls in retention | `call_state: "never"`, `last_call: null`, feed `state: "never"` | `[]` with no state; "idle" |
| Last call older than one interval | `call_state: "quiet"`, `silence_seconds` | hide the line; show the old call as if current |
| Agent never sent `status` | `status: null`, `status_state: "unreported"` | `status: ""`; omit the line |
| Status older than TTL | `status.stale: true`, `status_state: "stale"` | render as current |
| Heartbeat repeats the same status | no feed row | one row per heartbeat |
| Call has no resolvable agent | `agent_id: NULL`, counted in `unattributed_calls` | drop the row; assign to the only agent on the key |
| Call failed | row with `ok: false`, `error_code` | no row |
| Files reported, no lease | files kind `reported`; `file_state` unchanged | `leased`; move `file_state` |
| Feed write fails | call succeeds, warning logged | call fails |
| Retention swept | rows gone; `retention_days` on the payload | rows kept forever; rows gone with no hint why the feed is short |
| Sweep failed | call succeeds; warning logged; `agent_calls_sweep_failed` increments on `/health` | swallow and grow the table in silence |
| Call not attributable | no per-agent row; group line names the credential | a ghost agent; the only live agent on the key |

---

## 6. Data model

<!-- framing -->

**Migration `0102`.**

`agent_calls`:

| Column | Type | Note |
| --- | --- | --- |
| id | int pk | |
| ts | datetime tz, indexed | |
| project_id | fk projects, indexed | |
| agent_id | fk agents, nullable, indexed | D3 |
| api_key_id | fk api_keys | always set |
| source | str | `observed` \| `reported` |
| tool | str | |
| target | str | ≤120 |
| ok | bool | |
| error_code | str, nullable | |
| duration_ms | int, nullable | |
| status | str, nullable | reported rows |
| files | JSON, nullable | reported rows |

Composite index `(agent_id, ts)` for the feed read; `(project_id, ts)` for the sweep and summary.

`agents` + `status_text` (str, default `""`), `status_files` (JSON, default `[]`), `status_at` (datetime tz, nullable).

**Retention.** `AGENT_CALL_RETENTION_DAYS` in `config.py`, default 7. `agent_calls.record` runs `sweep(db, project_id)` every 200th insert per process (a module counter; not exact, not meant to be). `sweep` deletes `ts < now - retention` for the project. Never raises past `record` — but a failure is **counted** (D18): warning log, and `agent_calls_sweep_failed` on the `/health` payload next to the existing observability fields, so a sweep that keeps failing is visible on the deployed box rather than as a table that quietly grew. `POST /live/sweep` (admin, JWT) runs the same `sweep` by hand for the project. No scheduler.

**No change to `events`** beyond `meta.agent_id` on rows `_audit_tool` writes.

---

## 7. Acceptance criteria

<!-- framing -->

Each has a sabotage: revert the behaviour, confirm a test fails, restore. Both engines where the write or sweep hits SQL; view tests are frontend.

1. **Reads are rows.** `search_code` over MCP produces one `agent_calls` row with `source: "observed"`, the query as `target`, and **no** `events` row. *Sabotage:* gate the feed write on `_READ_ONLY`.
2. **Failures are rows.** A refused call (foreign item id → not_found) produces a row with `ok: false` and `error_code: "not_found"`. *Sabotage:* write only on the success path.
3. **Attribution by arg.** A call with `agent_id` is attributed to it. *Sabotage:* attribute to the key.
4. **Attribution by session.** A call with no `agent_id` on a session with exactly one live agent is attributed to that agent; with two live agents on the session it is `NULL`. *Sabotage:* pick the first.
5. **Unattributed is counted.** Rows with `agent_id NULL` appear in the board's `unattributed_calls` for that key's user group. *Sabotage:* filter them out of `summary`.
6. **Status on change only.** Two heartbeats with the same `status` produce one reported row; a third with a different `status` produces a second. *Sabotage:* write on every heartbeat.
7. **Presence-only is not a row.** `heartbeat` with no `id`, no `status`, no `files` moves `last_seen_at` and writes no feed row. *Sabotage:* record every heartbeat.
8. **Reported files do not lease.** An agent with `files: ["web/src/x.ts"]` and no reservation is `file_state: "unreserved"` with a `reported` kind in `files`. *Sabotage:* add `reported` to the priority table.
9. **Silence is named.** An agent with no rows has `call_state: "never"`, `last_call: null`; the feed endpoint returns `state: "never"`. *Sabotage:* `rows: []` with `state: "ok"`.
10. **Stale is labelled.** `status_at` older than `presence_ttl_seconds` → `status.stale: true`, `status_state: "stale"`. *Sabotage:* drop the comparison.
11. **No arguments stored.** Source scan: `agent_calls.record` is never passed `args`; the row has no JSON column but `files`. A test calls `add_memory` with a 2 KB text and asserts no row contains it. *Sabotage:* store `args` in `target`.
12. **Every tool has an extractor answer.** For every name in the manifest, `TARGETS.get(name, default)({}, None)` returns a `str`. *Sabotage:* an extractor that indexes `args["id"]` unguarded.
13. **Feed write never fails the call.** Monkeypatch `agent_calls.record` to raise; the tool call still succeeds. *Sabotage:* remove the guard.
14. **Retention sweeps.** Insert a row dated 8 days ago, force a sweep, it is gone; a 6-day-old row remains. Both engines. *Sabotage:* compare with the wrong sign.
15. **JWT only.** `GET /live/{id}/feed` with `X-API-Key` is 401. Another project's agent id is 404. *Sabotage:* `get_user_or_agent_key`.
16. **Board does not query the table.** `live.board` calls `agent_calls.summary`; the router calls neither. *Sabotage:* join in `board`.
17. **Activity names the agent.** An `update_item` over MCP with `agent_id` yields an `events` row whose `meta.agent_id` is set and whose Activity `agent` field is the agent id, not the key label. *Sabotage:* drop `meta.agent_id`.
18. **Producers say it.** `AGENTS.md` step 2 and the generated Cursor rule contain `status=`; `gbagent`'s timer heartbeat sends `status`. *Sabotage:* revert the prompt line; the rule-generation test fails.
19. **View states.** `live.test.tsx`: `call_state: "never"` renders "no calls recorded"; `quiet` renders "no calls for Nm"; `status_state: "unreported"` renders "no status reported"; `stale` renders the stale mark; a reported row and an observed row render with different marks. *Sabotage:* collapse the copy to one string.
20. **Two statements per board.** With 50 agents, `summary` issues two SQL statements (engine event hook count). *Sabotage:* loop per agent.
21. **Order-insensitive change.** Heartbeats with `files: ["a","b"]` then `["b","a"]` produce one reported row; then `["a"]` produces a second. *Sabotage:* compare lists.
22. **Sweep failure is counted.** Monkeypatch `sweep` to raise; the call succeeds and `/health` shows `agent_calls_sweep_failed: 1`. *Sabotage:* bare `except: pass`.
23. **No ghost rows.** Two live agents on one session, a call with no `agent_id`: neither agent's feed gains a row; the group's `unattributed_calls` is 1; the copy names the key. *Sabotage:* attribute to the first live agent.

Then run it against a real project with a real fleet — one `gbagent` worker, one Cursor agent on a shared credential, one unregistered caller — for ten minutes and **read the page**. If the feed is all `get_context` rows, that is the truth. If a status you know was sent is not on the page, it is not done. This is the operating-loop check, not a bonus pass.

---

## 8. Phasing

<!-- framing -->

**PR 1 — Observed feed.** D2 table + migration + `agent_calls.record` at the dispatcher, success and error paths. D3 attribution and `meta.agent_id` on Activity. D4 extractors. D10 retention. `GET /live/{id}/feed`. Board summary fields (D6) and the one-line row summary + expandable feed on `/live`. Silence states (D7, D11). No reported rows yet: `status_state` is `unreported` everywhere and says so.

**PR 2 — Reported status.** D5 heartbeat fields, `Agent` columns, on-change feed rows, `reported` file kind (D7). D12 producers: `AGENTS.md`, regenerated Cursor rule, `gbagent` timer heartbeat, Fleet mint prompt. Lands on PR 1 so a reported row has a timeline to appear in.

**PR 3 — Polish that does not add sources.** Collapse runs of identical observed rows ("get_context ×6"). A tool-class filter on the open feed (reads / writes / failures). Error rows link to the item they failed on. Still no hooks, still no push channel.

**Not this PRD.**

| Slice | Why later |
| --- | --- |
| Editor hooks posting observed writes | New ingest path: what counts as a write, JWT vs agent credential, retention, and the privacy of a keystroke-level trail. Separate grill. It lands rows with a third `source` value into this table. |
| SSE / websocket for the feed | PRD-33 D7 refused a second push channel for a board. Reopen only with a measured poll cost. |
| Model summary of a feed ("fixing the tests") | Needs the rows first, and a stated confidence. Must sit beside the rows, not replace them. |
| Org-wide feed across projects | Org plane is counts-over-`Event` (draft analytics PRD). A cross-project trail of people's agents is a different privacy decision. |
| Reported `files` as predicted reservations | Turns a claim into a lease with no write behind it. Refused in Non-Goals. |

---

## 9. Risks and open questions

<!-- framing -->

### Risks

1. **The feed is all reads and looks like noise.** A `claim_next` agent's ten minutes is `get_context`, four `search_code`, `get_item_details`. That is what it did. PR 3 collapses runs; nothing hides them. The alternative — write-only rows — is Activity, which already exists and already reads as idle.
2. **Reported status lies.** An agent can say "running tests" and be stuck. The mark says reported; the age says how long ago. Observed rows beside it say whether it called anything since. Do not render reported in the observed style to make the page feel authoritative.
3. **Volume.** A busy fleet of ten agents at one call every few seconds is on the order of a hundred thousand rows a day. Rows are small and indexed by `(agent_id, ts)`; retention bounds the table at about a week. If the write becomes measurable on the dispatcher's latency, batch it after the response the way deferred extraction already is (GRPH-399) — do not drop reads.
4. **Shared credentials stay ambiguous.** Two live agents on one session with no `agent_id` are `NULL`. The count on the board is the fix's prompt. Guessing would put one agent's queries under another's name.
5. **Surveillance creep.** The feed is a person's agents' queries and claimed files. JWT-only, project readers only, no MCP, no org roll-up, seven-day retention. Each of those is a line something will push on. Push back with this section.
6. **The sweep on the write path stalls a call.** Amortised, per-project, indexed by `(project_id, ts)`, and wrapped. If it shows up in latency, move the count threshold up before adding a scheduler.

### Open questions — closed 2026-09-03 (D22)

1. **Retention default.** **7 days.** Long enough to read yesterday's stuck agent; short enough that a busy fleet's table stays in the low millions. The setting exists for anyone who disagrees.
2. **Window for `calls_in_window`.** **10 × heartbeat interval.** The presence TTL is three intervals and too short to count anything. `call_state` (D21) uses one interval; the count uses ten; both are derived from the same payload clock.
3. **`status` on `update_item` / `claim_*`.** **No.** `update_item` already has `status` — the item's — and a second meaning on the same name is a footgun for every agent prompt. Heartbeat is the one call the loop makes on a timer; that is where "what am I doing" belongs.
4. **Reported `files` on the code graph.** **Live only.** PRD-20 D5 painted leases; a self-reported path glowing the same way is the lie PRD-20 refused. A later PRD may add a labelled fourth glow.
5. **Collapse identical rows.** **Client-side, keyed on `(tool, target)`** (D16). The table stays one row per call; the view folds runs. Server-side would change the row shape for a display concern.

Nothing in this PRD needs a prototype. Hooks remain a successor PRD, not an open question on this body.

---

## 10. Prior art

<!-- framing -->

- **PRD-33 §10 (agenttrail).** The declared-vs-observed split. This PRD is the *observed* half for the one thing Graphban can observe without a daemon — its own call stream — plus a *declared* status line. Editor writes are still the daemon's territory.
- **AL-43 Activity.** The audit ledger. The reason the feed is a different table: audit is forever and about mutations; telemetry is bounded and about everything.
- **GRPH-644 eval spans.** `LlmCallSpan` is Graphban's own model calls with tokens and latency, sampled into Memory review. Same instinct — write down what happened, label what was not measured (`tokens_source: "none"`) — applied to the agents' side of the wire.
- **PRD-19 E9a.** `mcp_session_id` on `Agent`, issued at `initialize`, used to trim the manifest. The same one-live-agent-per-session rule is what makes attribution without `agent_id` honest rather than a guess.
