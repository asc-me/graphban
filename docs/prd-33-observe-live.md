# PRD-33 — Observe Live: who is working this project

**Ledger id:** GRPH-P33
**Status:** approved — grill complete 2026-09-02; answers absorbed into D9–D17 and §9. v1.0.
**Depends on:** PRD-17 (Agent, presence, holdings) · PRD-19 (enrolment → key → user) · PRD-20 (held areas, group-by-human, `off_map`) · PRD-31 (Graphban does not fetch git)
**Complemented by:** Fleet view (provisioning) · Activity (audit ledger) · Code graph presence (spatial) · Dashboard (KPI tiles)
**Touches:** `web/src/components/shell/LeftNav.tsx` · `web/src/App.tsx` · `web/src/features/live/` (new) · `web/src/lib/api.ts` · `web/src/lib/queries.ts` · `web/src/lib/types.ts` · `backend/app/services/live.py` (new) · `backend/app/routers/live.py` (new) · `web/src/__tests__/p28-rail.test.tsx` · `web/src/features/docs/content.ts` · `web/src/features/projecthome/ProjectHome.tsx`

---

## 1. Overview

<!-- framing -->

A project with a running fleet has no page that answers, in one glance: **which humans have agents on this project, what those agents hold, which files they have reserved, and whether any of that work recorded a PR.**

The pieces already exist. `Agent` rows heart-beat. `ApiKey.user_id` is the human behind a credential. `AreaReservation` is the lease on files. `Item.pr`, `github_url`, and evidence URLs are the delivery receipts. `held_areas()` already joins reservation → agent → user. None of that is composed into an Observe board.

This PRD adds **Live** under Observe: a project-scoped status board, filterable by user, that reads those joins. It does not vendor [agenttrail](https://github.com/sodiumsun/agenttrail), does not watch anyone's disk, and does not list GitHub. v1 is a page over facts Graphban already writes.

The load-bearing invariant:

**Live reports what Graphban has measured. A missing measurement is a named third state, never a quiet empty.**

An online agent with no reserved files is `unreserved`, not idle. An item with no PR URL is `unrecorded`, not "no PR". An agent whose key has no user sits in **Unattributed**, not off the board. Those three silences are the same defect this codebase keeps rediscovering, wearing a roster.

### 1.1 What this is not

- **Not agenttrail.** Agenttrail is a localhost Node daemon that watches one repo's filesystem and a `PLAN.md` the agent maintains. Graphban is a server the agents call. Different identity, different runtime, different question. Steal the *declared vs observed* split; do not steal the daemon, the canvas, or the plan file.
- **Not a second code graph.** PRD-20 already paints leases onto nodes. Live is the *roster* of that fact, grouped by human, plus holdings and recorded PRs. The graph stays the spatial instrument.
- **Not Fleet.** Fleet mints seats and pastes prompts. Live watches work. Mixing them makes a provisioning page look like a status board, and a status board grow mint buttons.
- **Not Activity.** Activity is the audit ledger (AL-43): one row per accepted mutation, past tense. Live is who is here *now*.
- **Not Dashboard.** Dashboard is KPI tiles (`items_total`, status bar, MCP call count). Live is people.

---

## 2. Problem

<!-- framing -->

Verified against the tree, 2026-09-02, after reading [Noah's agenttrail post](https://x.com/noahinsf/status/2091704867211329752) and the current Observe / Fleet / presence surfaces.

### 2.1 The operator's question has no screen

The wanted hierarchy is:

```
project  →  user filter  →  users  →  agents  →  files they hold  →  recorded PRs
```

| Surface | What it answers | What it does not |
| --- | --- | --- |
| Observe → Activity | Who mutated what, historically | Who is here now; files; PRs as a feed |
| Observe → Memory / Lessons | Candidate inbox / published catalog | Live work |
| Build → Fleet | Roster, seats, roles, holdings (items), worktree, branch | Group-by-user; file leases; PR column; it is the minting page |
| Build → Code graph | Which *nodes* glow, with a per-human legend | The list of agents under a person; recorded PRs |
| Plan → Tracker | Items, `claimed_by`, `pr` on the detail panel | A project-wide feed |
| Dashboard | Counts | People |

The human who left four agents running and came back thirty minutes later currently triangulates Fleet + graph + tracker. Agenttrail's clip is popular because that triangulation is the pain.

### 2.2 "Files they are touching" is a lease, not a write

`AreaReservation` is written only by `claim_cluster` (`fleet.py`). `claim_next` and `next_cluster` write none. PRD-20 already recorded that the default all-in-one prompt used to say `claim_next`, which left the reservation table empty on a default install and made presence look idle.

A Live page that treats "no reserved files" as "not working" repeats that lie on a new screen. The graph at least has `off_map`. A list view that drops the unreserved agent is worse: it looks like a quieter, healthier fleet.

Graphban cannot see the agent's disk. Observed writes (agenttrail's fs watcher / Claude hooks) are a later reporting path, not v1.

### 2.3 "PR feed on the git repo" is not a thing we fetch

`PlatformConfig.github_connected` is inbound **issues**. `Item.pr` / `github_url` / evidence URLs are agent- or human-recorded. PRD-31 D12 / D18: Graphban does not probe git, does not infer `main`, and does not fill blanks from GitHub.

A live forge list needs a token, a reachable API, and a third state for "could not look". Collapsing "we did not look" into an empty feed is the absence rule again. v1 shows **recorded** PRs on holdings. Forge polling is an explicit later slice, not an empty panel labelled "PRs".

### 2.4 User is already joinable, and already dropped in one place

`held_areas()` does `Agent → ApiKey → User` (PRD-20 G4). FleetLegend groups by human: "someone running three windows is still one teammate." The Fleet *roster* does not — it is a flat list of agents. Live is the screen that grouping was waiting for, with a filter.

Agents whose key is gone, or never had a `user_id`, must not vanish. That is how a quieter board is manufactured.

---

## 3. Goals

<!-- framing -->

1. An Observe **Live** page for the **active project**: humans → their agents → holdings → reserved files → recorded PRs, with a user filter.
2. v1 reads existing tables. No new store, no new heartbeat field, no filesystem watcher, no GitHub list, no new env setting.
3. Third states travel by name, not by empty lists: `unreserved` / `idle` for files; `recorded` / `unrecorded` for PRs; `unattributed` for agents with no user.
4. JWT-only, same privacy posture as `GET /fleet/presence` (PRD-20): this payload names which human is on which files. Not on MCP in v1.
5. First PR leaves `main` deployable. An honest board that is mostly `unreserved` / `unrecorded` is acceptable. A board that looks idle because it dropped those rows is not.
6. Do not change `observeDefault`. Observe still opens on work waiting (Memory if review > 0, else Activity), not on the live board.

---

## 4. Non-Goals

<!-- framing -->

- Vendoring, embedding, or spawning agenttrail. No `PLAN.md` convention. No localhost daemon. No Claude-hook adapter in this PRD.
- A zoomable infinite canvas, run cards with current tool/todo, or session trails across components. That is agenttrail's product; the code graph is ours for space.
- Observing file *writes*. Heartbeat payloads, vendor hooks, and "leased vs written" heat are a later PRD. v1 labels leases.
- Fetching GitHub (or GitLab, or `gh`) for an open-PR list. `github_connected` is not a PR feed. A forge slice needs credentials, `unknown` / `unreachable` / `unconnected`, and must not ship as a quiet empty.
- Putting Live on Fleet, Dashboard, Activity, or the org-admin plane. Org analytics (draft, unnumbered) is counts-over-`Event`.
- An MCP tool in v1. Presence is JWT-only for a reason; Live is the same surface with more of the person attached.
- New settings. Poll cadence is `heartbeat_interval_seconds`, already on the fleet payload.
- Changing `observeDefault` to `/live`.
- Showing dismissed agents as live. Dismissed rows stay for provenance (PRD-17); Live hides them the way the roster does, and does not count them in `online`.
- Solving "is the agent stuck" with a new phase machine. Holdings already carry `phase` / `phase_basis` (GRPH-522). Live renders those; it does not invent a sixth.

---

## 5. Key decisions

<!-- framing -->

These are closed. Implementation of a slice must not quietly reverse one.

| # | Decision | Consequence |
| --- | --- | --- |
| D1 | New Observe sibling at `/live`, label **Live**. Not a tab of Activity, Fleet, or Dashboard. | Own empty state. Own filter. `observeDefault` unchanged. |
| D2 | Group by **human** (`ApiKey.user_id`), one chip/filter per user. Three windows stay one person. | Same grouping as FleetLegend. Filter is a query param, not a second page. |
| D3 | Files in v1 are **leases**. Kinds: `leased` · `predicted` · `off_map` · `unreserved` · `idle`. | `claim_next` agents stay visible. Predicted is labelled, never drawn as observed. |
| D4 | PRs in v1 are **recorded on holdings**. States: `recorded` · `unrecorded`. No forge fetch. | Empty is `unrecorded`, never "no open PRs". No PR panel on an idle agent. |
| D5 | One aggregation in `services/live.py`, one REST `GET /live`. JWT, `require_readable`. | Routers do not join. No MCP tool. Cap + `truncated`, no pagination of a live snapshot. |
| D6 | Unattributed agents are a first-class bucket, counted even when filtered out. | Dropping them is the sabotage the suite is written to catch. |
| D7 | Poll at `heartbeat_interval_seconds` from the payload, same as presence. | A faster poll is fake freshness. |
| D8 | No new table, no new write path, no new setting. | If a fact is not already stored, Live names it unmeasured rather than collecting it. |
| D9 | Truncation renders a banner **"N of M agents"** from `truncated` + `total_agents`. Filter chips still come from the full `user_counts` census. | No `dropped[]`, no cursor. The consumer knows exclusion happened, not who. |
| D10 | PR wire shape is **per-holding only**. `holdings[].pr = {state, url?}`. No agent-level `pr` in JSON. | Agent recorded/unrecorded/omit is a UI derivation from holdings. Mixed holdings stay mixed in the list. Idle = empty holdings, so no `pr` appears. |
| D11 | Filter is **one aggregation, two reductions**: census from the full set, `users[]` sliced by `?user=`. | Not two DB passes. Not client-only filtering — the URL is the source of truth. |
| D12 | Server derives `state` via `fleet.presence_state`. Client fades on `state === "offline"`. Echo `presence_ttl_seconds`. | Client does not recompute offline. Same TTL as the roster. |
| D13 | User join runs **every poll**. Chips follow the new payload. React key is `user_id ?? "unattributed"`. | A disappeared chip is the new truth. No stale chips, no sticky `user_id` column on Agent. |
| D14 | `GET /live` **fails closed: 500** if `list_agents` or `held_areas` fails. | No partial 200, no last-good cache. "A partial picture is worse than a slow one" means wait for the join. |
| D15 | `require_readable` on the project is enough, same as `GET /fleet/presence`. | Do not mask `user_id` for non-admins. Do not raise the bar to fleet-admin. JWT-only still stands. |
| D16 | Row `file_state` is the **dominant** kind per the D3 priority table. No `mixed` kind. | The files list still flags each predicted/off_map row. No count baked into the row label. |
| D17 | v1 closed: label **Live**; omit ghost `Item.touchpoints`; flatten sub-agents (do not drop children); sort viewer-first, then alpha, Unattributed last. | Nothing in v1 needs a prototype. Observed writes and forge feed stay successor PRDs. |

---

## D1 — The page, not a tab of Activity or Fleet

<!-- buildable -->

**Route.** `web/src/App.tsx` `PROJECT_VIEWS`: `["live", <LiveView />]`. Hosted: `projectPath(tag, "live")` via the existing helper — no ambient project, no literal `"/org"`.

**Nav.**

Self-host `OBSERVE` array (`LeftNav.tsx`):

```
Activity          /activity
Live              /live            (no badge — a board is not a queue)
Memory            /memory-review   (badge: review count, unchanged)
Lessons           /lessons
```

Live sits next to Activity because both are "what happened / is happening", and before Memory so the inbox/catalog pair stays adjacent. Do **not** change `observeDefault` (`review > 0` → `/memory-review`, else `/activity`).

Hosted `WORKSPACE` (flat; no Observe accordion): insert `{ to: "live", icon: Radar or similar, label: "Live" }` immediately after Activity. Import an icon `LeftNav` already has or `Activity` from lucide (Activity the icon, not the view). Do not put Live in Admin or Galaxy.

**Docs overlay.** `features/docs/content.ts`: register `/live` and a `/p/:tag/live` match the way `/activity` is registered. Related: Fleet, Code graph, Activity. Do not claim a forge feed or file-write heat exists.

**Project home.** `ProjectHome.tsx` `SURFACES`: add Live — "Who is on this project right now, what they hold, and whether a PR was recorded." Do not reuse Fleet's copy.

**Feature dir.** `web/src/features/live/` — not a subfolder of `fleet/` or `activity/`. Different job.

**Tests (nav).** `p28-rail.test.tsx` must not only `findByText("Observe")` — children are collapsed on `/tracker`. Required:

- render at `/live` (or `/activity`) and assert Activity, Live, Memory, Lessons;
- source grep that `OBSERVE` contains `to: "/live"` **and** `to: "/activity"` **and** `to: "/memory-review"`;
- hosted `WORKSPACE` has `to: "live"` immediately after `activity`;
- `observeDefault` is still not `/live`.

Wrap view tests in `<ProjectProvider>` inside a router; mock `api.projects` and `api.live`.

**Empty states, two of them, named.**

| Condition | Copy | Must not read as |
| --- | --- | --- |
| No agents on the project (and no unattributed) | "No agents have registered on this project." | "Everyone is idle." |
| Filter selected a user with zero agents | "No agents for this person on this project." | The project is empty. |
| Agents exist, all offline | Show them, faded, with last-seen. Offline is a state, not an empty. | Dropped from the list. |

A genuinely idle fleet (registered, online, holding nothing, no reservations) still renders the people. The rows say `idle`. That is the honest quiet.

---

## D2 — Group by human, filter by user

<!-- buildable -->

**Join.** `Agent.api_key_id → ApiKey.user_id → User`. Same path `held_areas()` already uses. Colour is `User.avatar`; initials are `User.initials`. No new palette.

**Unattributed.** `user_id` is null when the key is missing, revoked-and-gone, or never had a user. Those agents are grouped under a synthetic row:

```
user_id: null
label: "Unattributed"
```

The filter offers **All**, each distinct user who owns at least one non-dismissed agent on this project, and **Unattributed** when that bucket is non-empty. All-with-a-hidden-bucket is the failure: a filter UI that only lists named users makes unattributed unselectable and therefore easy to forget.

**Query param.** `?user=<user_id>` or `?user=unattributed`. One aggregation over the full board, then two reductions (D11): census (`user_counts`, `unattributed_count`) from the full set, `users[]` sliced by the filter. Not two DB passes. Not client-only filtering — the URL is the source of truth.

**Join at read time (D13).** `ApiKey.user_id` is re-read every poll. A revoked key or a changed user moves the agent on the next payload. A disappeared chip is the new truth. No client-side sticky chips, no `user_id` column on `Agent`.

**Sort (D17).** Users: the signed-in viewer first, then alphabetical by label, Unattributed last. Agents within a user: online before offline, then `last_seen_at` descending (nulls last — "no heartbeat yet" is not "just now"). Flatten `parent_agent_id`: a child is a second row, not nested, and is not dropped.

**Counts on the user row.** `online` / `total` for *that* user's agents on this project. "4 online" at the page level is forbidden as the only number — it is the same figure for four workers with no reviewer and for a balanced fleet (Fleet already taught this: counts by role). Live's page-level summary is **by user**, plus a role breakdown reused from `fleet_status.by_role` if cheap. Do not invent a new "4 agents online" tile.

**Sabotage.** Omit agents with `user_id is None` from the default (All) view. The test must fail. A board that got quieter by dropping orphans is the defect.

---

## D3 — Files are leases, labelled

<!-- buildable -->

Each agent row carries `files: []` and a **row-level** `file_state`. The row-level state is what the eye uses; the list is the drill-in.

| `file_state` | When | Meaning |
| --- | --- | --- |
| `leased` | ≥1 `AreaReservation` from declared touchpoints | The collision divvy reserved these paths |
| `predicted` | reservations exist, all `predicted=True`, none declared | Guessed by `predict_areas`; labelled, not observed |
| `off_map` | reservations exist, none resolve to a fresh `CodeNode` | Held, but the graph cannot place them (reuse `reason`: `undescribed` \| `stale`) |
| `unreserved` | agent `state` is not offline, holds ≥1 item, zero reservations | Working without `claim_cluster`. **Not idle.** |
| `idle` | online, holding nothing, zero reservations | Actually not in the code |
| `offline` | presence derived offline | Last leases may have expired; do not show stale areas as live |

Priority if several apply: `offline` > `leased` > `predicted` > `off_map` > `unreserved` > `idle`. A mixed declared+predicted set is `leased` (D16: dominant kind only — no `mixed`, no "leased · 4 predicted" on the row). Predicted rows still flagged in the files list. Do not average them into a new kind.

**Each file row.** `{ area, kind: "leased" | "predicted" | "off_map", reason?, node_paths?[] }`. `area` is the raw reservation string — the server still cannot tell `AGENTS.md` from `vercel env` (PRD-20). `node_paths` only when resolved.

**Reuse `held_areas` / `active_reservations`.** Do not query `AreaReservation` in the router or the view. Live's service calls the same function the graph uses, then groups by `agent_id`. Two readers, one lease clock: a dead agent's files lapse here when they lapse there.

**Do not fall back to `Item.touchpoints` as live files.** Touchpoints are declarative intent on the item, the thing PRD-20 refused to glow from. **v1 omits the ghost list (D17).** `unreserved` copy must say the agent holds work with no area lease, not that we looked at files and found none. A later PR 3 may add a distinct labelled kind (`declared on item, not reserved`); it must not mix into `leased`.

**Sabotage.** Render `unreserved` as `idle` (or hide the agent). The test must fail. This is the `claim_next` default-install hole, moved to a list.

---

## D4 — PRs are recorded, never fetched

<!-- buildable -->

A PR appears on Live only when Graphban already stored a URL on a **holding** of that agent.

**Sources, in order, first hit wins:**

1. `Item.pr` if it is a dict carrying a URL-like value (the column is JSON, nullable).
2. `Item.github_url` when `items.is_pr_url(github_url)` (already knows `/pull/`, `/pull-requests/`, `/merge_requests/`).
3. Any `item.evidence[]` entry whose `url` passes `is_pr_url`.

**States per holding:**

| State | When |
| --- | --- |
| `recorded` | A URL was found | payload includes `url` |
| `unrecorded` | The agent holds this item and no source hit | `url` absent, not `""` |

**Wire shape is per-holding only (D10).** `holdings[].pr = { state, url? }`. There is no agent-level `pr` field in JSON. The recorded / unrecorded / omit rule is a **UI derivation** from holdings: recorded if any holding is; unrecorded if holdings exist and none are; omit when `holdings` is empty (idle). Mixed holdings stay mixed in the list. An idle agent with `pr: []` looks like we listed the forge and it was empty — that is why the field is absent, not empty.

**Do not list PRs from `PlatformConfig.github_repo`.** Do not call GitHub. Do not use `Agent.branch` as a PR. Branch is shown on the agent row (already on the fleet roster) as *where the worktree is*, which is a different fact. A branch with no recorded URL is still `unrecorded`.

**Copy.** The column header is "Recorded PRs", not "PRs" and not "Open PRs". Unrecorded renders as the word `unrecorded`, not an em-dash, not "—", not blank.

**Sabotage.** (a) Render `unrecorded` as an empty list or "no PRs". (b) Fetch or stub a forge list and call a miss "no open PRs". Either fails the test.

---

## D5 — One aggregation, one service function

<!-- buildable -->

**Service.** `backend/app/services/live.py` → `def board(db, project_id, *, user_filter=None) -> dict`.

It composes, it does not duplicate:

- `fleet.list_agents` / presence state (dismissed excluded from the live set, same as the roster)
- `fleet.held_areas` grouped by `agent_id`
- holdings already on the agent dict (`phase`, `phase_basis`)
- PR extraction from those holding items (D4)
- User join from the key, same as `held_areas.holder`

The router does not join. The view does not join `/fleet` + `/fleet/presence` + `/items` client-side. A partial picture (roster without files, files without users) is worse than a slow one — same argument as `fleet_overview`. If `list_agents` or `held_areas` fails, **`GET /live` is 500 (D14)**. No `error: "partial"` body, no last-good cache.

**Router.** `backend/app/routers/live.py`, prefix `/live`.

```
GET /live?project_id=&user=
```

`authz.require_readable` is enough (D15), same as `GET /fleet/presence`. JWT via `get_current_user` only — not `get_user_or_agent_key`. Do not mask `user_id` for non-admins and do not raise the bar to fleet-admin. `test_cross_tenant.py` (or the live equivalent) asserts another project's board 404s.

**Payload (shape, not a second schema language):**

```
{
  served_at,
  heartbeat_interval_seconds,
  presence_ttl_seconds,
  truncated, total_agents,
  unattributed_count,
  users: [{ user_id, label, initials, color, online, total, agents: [
    { id, key, label, role, state, last_seen_at,
      worktree, branch, branch_orphaned,
      file_state, files: [{ area, kind, reason, node_paths }],
      holdings: [{ id, title, status, phase, phase_basis,
                   pr: { state: "recorded"|"unrecorded", url? } }] }
  ] }],
  user_counts: [{ user_id, label, online, total }]
}
```

`user_id` on the unattributed group is JSON `null`. Clients must not key React lists on `user_id` alone — use `user_id ?? "unattributed"`.

**Cap (D9).** Hard cap on agents scanned, `truncated` + `total_agents`. The view renders a banner **"N of M agents"**. Filter chips still come from the full `user_counts` census. No list of dropped agents, no cursor. Paginating a live snapshot renders half a fleet as the whole fleet.

**Fade (D12).** Client fades on `state === "offline"`. `presence_ttl_seconds` is echoed so last-seen is interpretable; the client does not recompute offline against it.

**Frontend.** `api.live(projectId, user?)` in `lib/api.ts`. `useLive(projectId, user)` in `queries.ts`, key `["live", projectId, user]`, `enabled: !!projectId`, `refetchInterval` from `heartbeat_interval_seconds` on first response (copy `useFleetPresence`, do not hardcode 15s).

**No MCP tool.** Documented in Non-Goals. A test that the tool name is absent is optional; a source scan that `TOOLS` does not gain `get_live` is enough if someone is tempted.

---

## D6 — Absence must not read as a quiet fleet

<!-- buildable -->

Named states, so they travel. Do not invent a parallel vocabulary later.

| Fact | Encoding | Forbidden shortcut |
| --- | --- | --- |
| Agent online, holds work, no `AreaReservation` | `file_state: "unreserved"` | hide row; `idle`; empty `files: []` with no state |
| Reservations exist but none match a fresh node | `off_map` + `reason` | drop the files; look idle on the graph *and* here |
| Reservations from `predict_areas` only | `predicted` | draw as `leased` / "touching" |
| Holding has no PR URL | `holdings[].pr.state: "unrecorded"`, no `url` key | `pr: null`; `url: ""`; "no PRs" |
| Agent holds nothing | empty `holdings` — no agent-level `pr` field | `pr: []` on the agent |
| Key has no user | group `user_id: null`, label Unattributed | omit from All |
| Filter is active | `user_counts` still lists everyone | chips only for the filtered user |
| Agent dismissed | absent from Live | shown as live; counted in `online` |
| Agent offline | shown, faded, `state: "offline"`, last-seen | dropped; shown as idle |
| No agents at all | empty-state copy in D1 | zero users with a status bar of zeroes |
| Payload truncated | `truncated: true` + `total_agents` | silent prefix that looks complete |

`files: []` without `file_state` is invalid. The view must not infer idle from an empty array. Pin this with a fixture that has `file_state: "unreserved"` and `files: []` — the row still says unreserved.

**Served-at.** Echo `served_at` so last-seen age is computed against the payload, not the browser clock, same as presence.

---

## D7 — Poll on the fleet clock

<!-- buildable -->

Copy `useFleetPresence`: start conservative, then set `refetchInterval` from `heartbeat_interval_seconds * 1000`. Presence is only as fresh as the heartbeat that feeds it. Asking faster renders a confidence we do not have.

Offline is derived server-side by `fleet.presence_state` (PRD-17). The view fades on `state === "offline"` (D12); it does not re-apply TTL. Live must refetch even when the user does not interact, or a dead agent stays "12s ago" until navigation. `refetchInterval` is the whole of that. No new websocket. No SSE. Graphban does not grow a second push channel for a board.

`prefers-reduced-motion`: no pulse on live dots (PRD-20 G6 / acceptance walk). Presence must not vanish for someone who asked for less motion — dim, do not hide.

---

## 6. Data model

<!-- framing -->

**No migration.** v1 is a read model over:

| Table / function | Field | Role on Live |
| --- | --- | --- |
| `Agent` | id, label, role, state, last_seen_at, worktree, branch, dismissed, api_key_id, parent_agent_id | row |
| `ApiKey` | user_id | grouping |
| `User` | initials, avatar, display name | chip |
| `fleet.active_reservations` / `held_areas` | area, predicted, off_map reason, node_paths | files |
| `Item` (holdings) | status, phase, pr, github_url, evidence, touchpoints | holdings + recorded PRs |
| presence TTL / lease seconds | already on fleet payload | poll + offline |

`parent_agent_id` is flattened in v1 (D17): a child is a second row under the same human, not nested, and is not dropped. Nesting is later polish.

---

## 7. Acceptance criteria

<!-- framing -->

Each has a sabotage: revert the behaviour, confirm a test fails, restore. Both engines where the join hits SQL; the view tests are frontend.

1. **All includes unattributed.** An agent whose key has `user_id=None` appears under Unattributed on the unfiltered board. *Sabotage:* `if not user_id: continue`.
2. **Filter does not erase the rest of the census.** With `?user=` set, `user_counts` still lists other users and `unattributed_count` is still the real count. *Sabotage:* compute counts from the filtered set.
3. **`unreserved` ≠ `idle`.** Online agent, one holding, zero reservations → `file_state="unreserved"`. *Sabotage:* empty files ⇒ idle.
4. **Predicted is labelled.** A reservation with `predicted=True` and no declared sibling is `predicted`, not `leased`. *Sabotage:* drop the flag.
5. **`off_map` is present.** A reservation that matches no fresh node appears as `off_map` with `reason`. *Sabotage:* skip unresolved areas.
6. **Unrecorded is a word.** A holding with no PR source renders `unrecorded` and has no `url` key. *Sabotage:* `url=""` or copy "no PRs".
7. **Idle agents omit `pr`.** Holding-nothing ⇒ no `pr` field on the agent. *Sabotage:* `pr: []`.
8. **JWT only.** `GET /live` with an `X-API-Key` is 401. Another project's id is 404. *Sabotage:* `get_user_or_agent_key`.
9. **Nav.** Self-host Observe contains Live next to Activity; hosted WORKSPACE has `live` after `activity`; `observeDefault` is not `/live`. *Sabotage:* add Live and also land Observe on it.
10. **Truncation is stated.** Over-cap payload has `truncated=true` and `total_agents` > `len(agents)`. *Sabotage:* slice without the flag.
11. **No forge client.** Source scan: `services/live.py` does not import `httpx` / `urllib` / GitHub URLs as a fetch. *Sabotage:* list PRs from `github_repo`.
12. **One service.** The router calls `live.board`; it does not query `Agent` itself. *Sabotage:* join in the router.
13. **Truncation banner.** `truncated=true` renders "N of M agents"; `user_counts` is still the full census. *Sabotage:* hide chips or omit the banner.
14. **Fail closed.** A held_areas failure is HTTP 500, not a 200 with a partial `users` list. *Sabotage:* catch and return `[]`.
15. **No agent-level `pr`.** The JSON has `holdings[].pr` only. *Sabotage:* add `agent.pr`.

Then run it against a real project with a mixed fleet (one clustered agent, one `claim_next` agent, one key without a user, one holding with a PR URL, one without) and **read the page**. A green suite that still shows a quiet board is not done. This is the operating-loop check, not a bonus pass.

---

## 8. Phasing

<!-- framing -->

**PR 1 — Board that cannot lie.** D5 endpoint + D2 grouping + D3 file_state + D4 recorded/unrecorded + D6 encodings + D1 page with All / user filter. May look sparse on a `claim_next` project. Must not look empty.

**PR 2 — Nav, docs, hosted rail, Project home, reduced-motion.** D1 leftover chrome. Lands on PR 1 so `/live` is not a 404 from the rail.

**PR 3 — Polish that does not add sources.** Optional nest of `parent_agent_id`, declared-on-item ghost list under `unreserved` (labelled, distinct kind), role counts on the page header. Still no watcher, still no GitHub.

**Not this PRD.**

| Slice | Why later |
| --- | --- |
| Observed file writes (hooks / heartbeat paths) | New write path. Declared vs observed is the agenttrail insight; it needs agents to *report* writes. Separate grill: what is a write, retention, JWT vs agent ingest. |
| Forge PR feed | Needs credentials, third states (`unconnected` / `unreachable` / `unknown`), and a decision against PRD-31's "Graphban does not fetch git". Empty-as-none is the trap. |
| MCP `get_live` | Privacy: presence is JWT-only. Reopen only with a redacted agent-facing shape (no `user_color` / initials? or no users at all). |
| agenttrail-style canvas / run cards / current tool | Vendor session telemetry we do not store. |

Each later slice is a successor PRD or a rebaseline that **adds a section**. Do not grow this body's Goals to hold them; that would make a completeness pass demand a forge feed this draft explicitly refused.

---

## 9. Risks and open questions

<!-- framing -->

### Risks

1. **The board is honest and looks broken.** On a default all-in-one fleet that `claim_next`s, every row is `unreserved` / `unrecorded`. That is the truth and it will get a bug report. The fix is not to guess files; it is to keep the words, and to keep Fleet's prompt on `claim_cluster`.
2. **Two screens disagree on "who is here".** Live vs Fleet vs graph. They must share `presence_state` and `held_areas`. A third derivation in `live.py` will drift within a week.
3. **Live becomes the minting page.** Pressure to "start an agent from here". Refuse — that is Fleet. A button that jumps to `/fleet` is allowed; a mint form is not.
4. **Forge creep.** A weekend `httpx` to `github_repo` will ship the empty-as-none bug unless D4's scan stays. Keep the source scan (A11).
5. **Privacy.** The payload is a map of people onto files. JWT-only is load-bearing. Hosting this on MCP "so agents can coordinate" reopens PRD-20's surveillance decision.

### Open questions — closed 2026-09-02 (D17)

1. **Label.** **Live.** Agents collides with Fleet. Presence collides with the graph legend.
2. **Ghost touchpoints in v1.** Omit. PR 3 may add a distinct labelled kind, not mixed into `leased`.
3. **Sub-agents in v1.** Flatten. Do not drop children. Nesting is polish.
4. **Sort.** Signed-in viewer first, then alpha, Unattributed last. Not most-online-first.

Nothing in v1 needs a prototype. Observed writes and a forge PR feed remain successor PRDs, not open questions on this body.

---

## 10. Prior art — agenttrail

<!-- framing -->

[agenttrail](https://github.com/sodiumsun/agenttrail) (MIT, `npx agenttrail`, v0.2.0, 2026-08) is a local observability map: fs watcher + optional Claude Code hooks + a `PLAN.md` component graph. It binds `127.0.0.1`, has no users, no orgs, no PRs, and no server. Its load-bearing trick is **declared (plan/todos) vs observed (writes)** — a completed card lights up when its files change again.

Graphban already has the better *declared* signal: `AreaReservation` with expiry, `predicted` vs actual, `off_map` when the graph cannot place it, and `User ← ApiKey ← Agent`. What it lacks is observed writes, and a page that lists the join by human.

Vendoring the daemon would attach a Node process to every agent host and a second plan file next to the tracker. That is a different product. This PRD takes the trick (label declared vs missing-observed) and implements it on Graphban identity.

A later PRD that ingests reported writes is how "files they are touching" becomes observed. It is not how v1 ships.
