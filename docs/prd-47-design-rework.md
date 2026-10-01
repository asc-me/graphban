# PRD-47 — The design rework: the twenty surfaces the design names

**Ledger id:** GRPH-P47 — created in the ledger 2026-09-26. The ledger is the source of truth; this file is a review copy and must agree with it (`backend/tests/test_prd_sync.py`). Committing it means regenerating `docs/prd-index.json` with `scripts/gen_prd_index.py`.
**Status:** draft — not yet grilled. Approval is earned by finishing the grill, never set.
**Source of truth for the visuals:** the Claude Design project `7082bfe3-256f-4f66-9a8b-1a3a51fc5488`. Its `*.dc.html` files are the designs; `gb-sync/**` and `gb-sync/_ref/**` are machine captures of the app as it renders today (`scripts/design-capture/`), not proposals. Where this PRD and a design disagree, the design wins on layout and copy; this PRD wins on what the server can actually answer, and a shipped decision wins over both (see §1.3).
**Sits on:** GRPH-P46 *Local UI craft refresh* (approved v1.0, 2026-09-23) — tokens, type scale, contrast, the motion system, primitive states, the command palette, login, the a11y floor, shell-chrome craft, and the four named states on the planner's daily five. **This PRD does not restate any of that.** See §1.2.
**Also depends on:** GRPH-937/938/940 (grain and elevation, Plex Sans for chrome, the hosted section rail) · `web/src/components/planner/PlannerStates.tsx` · PRD-38/PRD-41 (Harness telemetry, the capability grid) · PRD-16 (lessons) · PRD-21 (the org plane) · PRD-43 (public surfaces)
**Touches:** `web/src/features/**` · `web/src/components/planner/PlannerStates.tsx` · `web/src/components/shell/LeftNav.tsx` · `web/src/lib/queries.ts`, `api.ts`, `types.ts` · `backend/app/routers/{events,usage,settings}.py` and their services, for S10, S14 and S15 only

---

## 1. Overview

The design set covers the whole application: three section mocks (`Plan`, `Build`, `Observe`) that each carry five or six pages, and eleven deep single-surface mocks (`Project Home`, `Memory`, `Lessons`, `Activity`, `Live`, `Harness`, `Feedback Kit`, `Usage`, `Log Export`, `Org Link`, `Sync Link Settings`). Twenty product surfaces.

The chrome is already there, or already spoken for. GRPH-937/938/940 shipped the material, the type and the section rail; the top bar already has the live-tools chip, a real command palette behind ⌘K and a named jump field; `PlannerStates.tsx` already defines the four-state vocabulary; and GRPH-P46 owns everything systemic that is still outstanding. What is left is the part no approved PRD covers: **reach and content**.

- Seven views use the state vocabulary. Thirteen do not, and GRPH-P46 scopes only five of them.
- Three surfaces in the design do not exist: Usage, Log export, and the Feedback Kit setup tab.
- Four surfaces are a fraction of what is drawn: Activity, Memory, Lessons, and the Harness guidance tab.

So this is a per-surface build-out against a design that already exists, not a re-theme and not a design system.

### 1.2 Relationship to GRPH-P46, stated so the overlap is reviewable

GRPH-P46 is approved and covers the systemic layer. Written out, because two PRDs quietly specifying the same thing is worse than either:

| Concern | Owner |
|---|---|
| Tokens, type scale, radius, elevation, contrast floor | P46 §5 — **not here** |
| Motion tokens, reduced motion, press, overlay paths | P46 §6 — **not here** |
| Button / Input / Dialog / Menu / Tabs states | P46 §7 — **not here** |
| Command palette behind ⌘K | P46 §8 — **not here** |
| Login and first-run | P46 §9 — **not here** |
| Four named states on Home, Tracker, PRDs, Memory review, Requests | P46 §10 — **not here** |
| Shell-chrome *craft*: chip contrast, accessible names, skip link, agent-rail motion | P46 §11–12 — **not here** |
| The same four states on the other thirteen surfaces | **S1, here** |
| Nav destinations the design adds (Usage, Feedback Kit, hosted Harness) | **S2, here** |
| What each of the twenty surfaces actually shows | **S3–S17, here** |

Where P46 says a surface "gets the shared header/state treatment when it is free" (§10, of Fleet / Live / Activity / Harness), S1 is that work, made explicit and tested rather than left to spare capacity.

### 1.3 What this is not

- **Not a palette change.** The tokens in `web/src/index.css` stay, and changing them is P46's business, not this PRD's. The two-theme toggle in `gb-sync/pages/plan-dashboard/index.html` was a comparison card for picking a palette; that choice is made and committed.
- **Not a regression of GRPH-938.** The designs draw chrome labels as tracked-out uppercase IBM Plex Mono — the live-tools chip as `MCP · 24 TOOLS LIVE`, section labels as `OBSERVE · MEMORY`. GRPH-938 deliberately moved chrome to Plex Sans, sentence case, mono reserved for IDs and keys, and `web/src/__tests__/chrome-font-sans.test.ts` holds that line. **The shipped decision wins.** Adopt the design's layout and information, not its casing or its font for chrome. This is the one place the design set is out of date, and it is out of date on purpose.
- **Not the `STATE` chip row.** Every section mock has a `POPULATED / EMPTY / LOADING / ERROR` strip in the breadcrumb bar. That is the design tool's own variant switcher. It must not ship.
- **Not the mock data.** Ana Silva, Marco Reyes, `wave-7`, `otel.ascme.internal` and the 1,284-event histogram are fixtures. Every number on a shipped page comes from the server or is absent.

#### Divergences between the designs and what shipped

Recorded here per **D7** rather than resolved in a PR. The design-vs-built pass over the thirteen deep surfaces found these four. Each names its basis and what holds the line, so the next reader does not re-file one as a defect — and so nobody "fixes" one by inventing the column, the control or the axis that is missing. A fifth divergence found later is added to this table, not decided in a diff.

| The design draws | What shipped | Basis | Held by |
|---|---|---|---|
| Activity's event panel: a field-level diff, and a trace id to copy and pivot on | A **Not on the record** section carrying two labelled `NotRecorded` seams, one per absent field | The event record has no diff column and stores no trace id, and nothing writes either. A blank where a diff should be reads as *nothing changed*; a blank where a trace id should be reads as *no trace*. Both are claims the record cannot support, which is **G3**. | `web/src/features/activity/EventPanel.tsx`, asserted by `web/src/__tests__/activity.test.tsx`. Deferred with the measurements as **GRPH-979** rather than drawn as a plausible blank. |
| Sync/Link's mapped-projects row: **attach** and **detach** | The row, with neither control | The only primitive the backend exposes is `syncSetGraph` (`PATCH /platform`), which toggles graph push for a project that is *already* mapped. Nothing attaches a project to the org or detaches it, so either control would be a button that cannot work. | The doc comment on `ProjectsTable` in `web/src/features/settings/SyncLinkPanel.tsx` — a comment and not a test, so the absence is only as durable as that file. Belongs with the endpoint: **GRPH-986**. |
| Harness's performance grid keyed on three function sets — `UI & components`, `API & services`, `Data & migrations` — as drawn in `Harness.dc.html`, labels that live in the design project and are not vendored into this repo | The same grid keyed on vendor × model × **capability** × size band, capability being PRD-41 §5's id (`B5 React component and state`, `B1 REST and contract surface`, `A4 Schema and migration`) | **A dependency, not a decision.** The axes are PRD-41's, which is approved and only part-shipped, and its ids *never change meaning once shipped, because a cell's history depends on them*. The design's three labels are not PRD-41 names, so re-keying onto them would detach every cell from the attempts that fill it. | `HarnessCellKey` in `web/src/lib/types.ts`; the enum is served as `capability_set` on `GET /api/harness` (`backend/app/routers/harness.py`, `backend/app/services/harness.py`) rather than hard-coded. Unblocks when **PRD-41** lands. |
| Memory's keyboard hint — recorded in S8 below as `J/K move · X select · Enter first action`, and drawn in `Memory.dc.html` with a `⏎` where S8 writes `Enter` | The handler has always bound those keys; the hint had not shipped, so the affordance was undiscoverable. **Shipped by this audit** as `J/K move · X select · Enter open` | §2.2 asks for keyboard-first selection, and a shortcut nobody can discover is not keyboard-first. Lessons and Activity already ship the same line, so Memory was the outlier rather than a deliberate omission. | `web/src/features/memory/MemoryTriageView.tsx`; `web/src/__tests__/memory-triage.test.tsx` asserts the hint renders *and* that each key it names does what it says. The wording stays *Enter open* and does not adopt the design's *first action*: Enter opens the detail panel, and Memory has no default bulk action to take, so the design's label would misdescribe it (**G3**). |

The first two are the good case and are the shape the rest should follow: a gap declared on screen in the shape of the thing that is missing, which is **G3** working rather than failing.

## 2. Problem

### 2.1 Absence still reads as clean on thirteen surfaces

This is the repository's recurring defect class, and the design names it in copy on nearly every page: *"The catalog has not been served. That is not an empty matrix."* · *"That's not a list of zero machines."* · *"No outcomes recorded. Nothing links this lesson to a check, so a hit can't be told apart from noise."* · *"Empty because nothing has arrived, not because GRPH has no structure."*

Measured 2026-09-26: `PlannerStates.tsx` is imported by seven views (tracker, requests, PRDs, roadmap, memory review, live, lessons). Dashboard, Triage, Activity, Harness, Code graph, Links, Fleet, Outposts, MCP Tools, Feedback Kit, Project Home, Org overview and Galaxy import none of it, and render a failed fetch and an empty project identically. `DashboardView` and `ActivityView` have no error branch at all; `FeedbackKitView` has neither error nor loading.

The design-capture render check already flags three of these in red on their cards. The flags are correct.

### 2.2 Four surfaces are a sketch of what is drawn

- **Activity** is 130 lines: a flat, unfiltered, unsearchable list with no error state. The design is a lens picker, a 48-bucket clickable histogram, person/surface/object facets, grouping by time or target, an event panel with the actor chain, the field diff and the trace id, CSV export and `J/K` navigation.
- **Memory** and **Lessons** are single lists. The design is a queue picker (five and six queues), a bulk cleanup sweep with a preview, a canonical-wording picker for near-duplicates, an outcome strip for the last sixteen times a lesson surfaced, and keyboard-first selection.
- **Harness** has the performance grid and the probe panel (PRD-41 shipped those) but not the *Guidance* tab — the routing table gbfleet is actually served, the grading rules that turn a cell into a verdict, and the verbatim `fleet_status` text — and not the effort curve.

### 2.3 Three surfaces do not exist

**Usage** (deployment-wide MCP calls, seats, per-project and per-model spend, busiest keys, license limits), **Log export** (OTLP collector configuration, signal selection, redaction, sample record), and the **Feedback Kit setup tab** (how a private host becomes reachable — relay, custom domain, DNS records, readiness checks). The first two need endpoints that do not exist.

### 2.4 Two destinations the design shows are unreachable

The nav footer has Organization, Operator and Settings. The design adds **Usage** and **Feedback Kit** beside them; Feedback Kit exists only as a Settings sub-page, and Usage does not exist. Separately, `Harness` is in the self-host Observe rail and missing from the hosted one, so the same deployment shows a different Observe depending on mode.

## 3. Goals

- **G1 — The state floor reaches everything.** Every surface in §2.1 distinguishes loading, error, empty and filtered-empty, using `PlannerStates.tsx`, with the design's copy. A failed fetch never renders as an empty project.
- **G2 — Each of the twenty surfaces matches its design** in layout, information and copy, to the fidelity the server can support — and matches GRPH-938's typography where the two disagree (§1.3).
- **G3 — Nothing is invented.** Where a design shows a number the server cannot produce, the slice either adds the endpoint (S10, S14, S15) or ships the panel with an explicit "not measured" state. It never fabricates a value, and it never computes a percentage against a limit nobody declared.
- **G4 — Every destination the design draws is reachable**, in both hosted and self-host mode, with the same Observe rail in each.

### Non-goals

- Anything in the §1.2 table marked "not here". A PR in this arc that edits `index.css` tokens, the motion layer or `components/ui/*` is out of scope and belongs to P46.
- Re-theming, a light theme, or new tokens.
- Mobile layouts. The designs are 1440x900; existing responsive behaviour is kept, not extended.
- The `STATE` variant chips (§1.3).
- Adopting the design's uppercase-mono chrome labels (§1.3).
- Cloud-side billing or plan enforcement. S16 builds the dialog the design draws; what it posts to is PRD-21's existing link flow.

## 4. Key decisions

<!-- framing -->

- **D1 — The design set is read as three layers.** `*.dc.html` = intent. `gb-sync/**` = the app as it renders today. `gb-sync/_ref/**` = the reference material the captures were built from. Only the first is a specification. `gb-sync/_ref/pages/build-harnesses-proposed/` is a proposal, and `scripts/design-capture/README.md` says never to overwrite it.
- **D2 — `PlannerStates.tsx` is the only vocabulary.** New surfaces add skeleton exports to that file, not local copies.
- **D3 — Copy comes from the design verbatim** where the design writes it, because that copy is the mechanism for G1: it is what distinguishes "nothing arrived" from "nothing exists". Casing and font follow GRPH-938, not the mock.
- **D4 — Backend work is confined to three slices.** S10 (Activity: facets, buckets, diff, trace id), S14 (Usage aggregates), S15 (OTLP export config). Every other slice is frontend-only and can run in parallel.
- **D5 — A panel that cannot be fed is not drawn.** The design's Usage page prices model spend from reported tokens; PRD-38 attempt records carry tokens, so that column is real. It also shows `Seats in use 18 / 25` — where the license declares no seat cap, the row says so rather than dividing by a guess.
- **D6 — S1 ships first.** It touches thirteen files shallowly and removes the defect class that the rest of the rework would otherwise re-introduce surface by surface, in thirteen separate code reviews.
- **D7 — The design's own divergences are recorded, not silently resolved.** §1.3 is the list. If a reviewer finds a second one, it is added there rather than decided in a PR.

## 5. The design file to surface map

<!-- framing -->

| Design file | Surfaces it specifies | Slice |
|---|---|---|
| `Plan.dc.html` | tracker, requests, triage, dashboard, PRD list, PRD editor, roadmap | S3, S4 |
| `Build.dc.html` | links + code graph, MCP tools, fleet, outposts | S5, S6, S7 |
| `Observe.dc.html` | memory, lessons, activity, live — at section level | S8-S11 |
| `Memory.dc.html` | Observe to Memory, in depth | S8 |
| `Lessons.dc.html` | Observe to Lessons, in depth | S9 |
| `Activity.dc.html` | Observe to Activity, in depth | S10 |
| `Live.dc.html` | Observe to Live, in depth | S11 |
| `Harness.dc.html` | Observe to Harness, three tabs | S12 |
| `Feedback Kit.dc.html` | Feedback Kit: customize + setup | S13 |
| `Usage.dc.html` | Usage — new | S14 |
| `Log Export.dc.html` | Settings to Log export — new | S15 |
| `Org Link.dc.html` | the cloud-org link dialog | S16 |
| `Project Home.dc.html`, `Sync Link Settings.dc.html` | Project Home, Settings to Sync/Link | S17 |
| every file | the nav footer and the Observe rail | S2 |

## S1 — The state floor on the thirteen surfaces P46 does not cover

Apply `PlannerError`, `PlannerEmpty`, `PlannerFilteredEmpty` and a skeleton to Dashboard, Triage, Activity, Harness, Code graph, Links, Fleet, Outposts, MCP Tools, Feedback Kit, Project Home, Org overview and Galaxy. Add the shapes they need to `PlannerStates.tsx`: a KPI-grid skeleton (Dashboard, Usage), a two-column queue skeleton (Triage), a table skeleton (Harness, Usage), a card-grid skeleton (Project Home, Outposts), an event-list skeleton (Activity).

Copy, from the designs:

- error, every surface: *"The request failed — nothing here means empty."* with a Retry control.
- Triage queue empty: *"Everything reported has been triaged. Not the same as nothing having been reported — history lives in the tracker."*
- Triage clusters empty: *"Nothing is claimed, so there is nothing to collide. This is an idle project, not a cleared one — the check found no work rather than no conflict."*
- Harness empty: *"Nothing measured yet. A cell appears here once a delegation finishes — one row per vendor, model, capability and size band. Until then gbfleet gets no guidance and resolves every tier by matrix order and policy alone."*
- Harness catalog failure: *"The catalog has not been served. That is not an empty matrix."*
- Outposts empty: *"That's not a list of zero machines. Nothing has registered yet. Install gban on a machine and it appears here the first time an agent on it calls register_agent."*
- Fleet roster empty: *"No agents yet. One agent needs an API key in Settings and no seat. A fleet issues seats on Wave — a seat is the role, not a second key."*
- Project Home, no code graph: *"No deployment has pushed a code graph to this project. Empty because nothing has arrived, not because GRPH has no structure."*

Acceptance: per surface, a test that puts the query in `isError` and asserts the retry affordance is present *and* the empty-state title is absent. The second half is the one that catches the defect. Reviewer sabotage: force the fetch to fail and confirm the page does not read as an empty project.

## S2 — The destinations the design adds

- Nav footer gains `Usage` and `Feedback Kit`, beside Organization / Operator / Settings, in both modes.
- The hosted Observe rail gains `Harness`, so hosted and self-host show the same section.
- The breadcrumb bar carries `ORG / Project GRPH / SECTION · PAGE` with its accent stripe on every project surface, in Plex Sans per GRPH-938 — and no `STATE` chips.
- Feedback Kit becomes a destination in its own right, not only `/settings/project/feedback-kit`; the existing settings path redirects to it so no bookmark breaks.

## S3 — Plan: tracker rows, requests, triage clusters, dashboard

- **Tracker row** gains: the `PROTO` badge for `fidelity=high`, tags, effort, the PR chip (number, state colour, check colour, tooltip `PR #n · state · checks c`), the owner chip with a claim dot, and click-status-to-advance.
- **Requests**: vote count, type chip, expand-in-place detail, the publish toggle with its two labels, and the linked-code list with `no linked code yet` when there is none.
- **Triage**: the incoming queue with duplicate detection (`looks like GRPH-924 — 91% similar`), and collision clusters with `blocked` vs `serialize` risk, the `PREDICTED` badge, shared paths, and the explain sentence per risk — blocked reads *"the overlap is already held, so a second claim would be refused rather than queued"*; serialize reads *"Running them together is what produces the conflict this check exists to predict."* Keep the scope note: *"Clustering reasons over this project's code graph only. Overlaps across repos are not computed — a shared package name is a galaxy edge, not a collision."*
- **Dashboard**: the six KPIs with icons, the status-distribution segmented bar with legend, requests-by-type bars, recent activity, and the dismissible agent-loop card — plus the error and empty states it has never had.

## S4 — PRD editor: tabs, coverage, acceptance, history

Six tabs — preview, assistant, grill, coverage, acceptance, history — over the existing editor. The grill tab shows `n / m answered` and the line *"Approval is earned, not picked — Approved unlocks when every question has an answer the eval accepts."* Coverage lists each goal and acceptance id against the items covering it, with `uncovered` called out and a decompose action. Acceptance shows delivered / nothing-delivered / `CUT FROM SPEC`. History lists versions with their notes. The AI commands (Expand, Generate risks, Summarize) sit in the editor toolbar. Per P46 §10 the grill and acceptance panels are product, not chrome: this slice gives them a home, not a redesign.

## S5 — Build: MCP tools and the graph inspector

- **MCP tools**: the connect-an-agent block with the copyable snippet and its per-client variants; *The loop, by role* — the ordered call sequence per role, the why for each step, and what to avoid; and the searchable reference, grouped, with a detail panel per tool (tier, call count, when to use, example, params with required flags, returns, roles, `Refused when …`, `Usually followed by …`) and the miss copy *"No tool matches. Try what you want to do, e.g. 'review' or 'files'."*
- **Graph**: the Hubs panel ranked by what depends on a node, the inspector with kind/area/description, Zoom-to / Clear / Path-from-here, the trail breadcrumb, neighbour groups by relation, and the hover-preview / click-pin / double-click-zoom hint with area jump cards.

## S6 — Build: Fleet wave provisioning, review queue, clusters, catalog, allocation

- Roster with offline agents faded rather than removed (*"one that died holding a branch is what you need to see"*), the credential kind, un-enrolled and orphan markers, the last refusal, role/state/heartbeat, held areas with a dismiss, and a `Show n gone` control.
- **Provision a whole wave**: the seat planner (+worker / +planner), the plan summary, and the copy that explains a seat against a key — *"A seat grants a role for one session and expires — paste it into the prompt, not the config"* — and what ending a wave revokes.
- **Review queue** showing who built each item, *"the reason it needs somebody else"*, and **Clusters** showing non-colliding work with the reason anything held back is waiting.
- **Harness catalog** — what `spawn(tier=)` can resolve to, with *"Status moves by a commit, not by this page"* — and **Allocation**: mix by share, the off-state explanation, unregistered adapters not offered a share, and `No mixable harness in the catalog.`

## S7 — Build: Outposts

Host cards with OS, online count, the fix hint, and per-agent rows (key, model, via, vendor tier, branch, state). The host-definition footnote: *"A host is whoever registered an agent with a host capability or a label of the form model @ host. 'Unspecified' is a real group, never silently localhost. Tool versions are what the host last declared."* Plus *Three tools, one machine* (gban / gbfleet / gbagent — what each is for, where it runs, and that gbagent ships with gbfleet) and *Set up this machine*: install-method and adapter pickers over copyable numbered steps with their expected output.

## S8 — Observe: Memory triage

Five queues (needs, conflicts, dupes, stale, unvetted) with counts and hints; search, sort and facet chips; a **cleanup sweep** with Preview before the action; bulk selection with a select-all box and a batch action bar; a row table (text, id, origin, class, confidence, age); `J/K move · X select · Enter first action`; Load-50-more. The detail panel carries the shard text, metadata, the class picker, the published shard it contradicts, the **canonical-wording picker** for near-duplicates (*"Pick the canonical wording"*, *"Publish as principle · drop 2"*), and Publish / Reject / Ask-the-judge with an undo toast. Keep the project-policy strip and its pointer to project preferences, and the queue-clear copy *"Queue clear. Pick another queue above, or search everywhere."*

## S9 — Observe: Lessons upkeep

Six queues (dropping, missed, unmeasured, overlap, promote, unclassified) with the same queue / sweep / bulk / keyboard machinery as S8, but scored on whether the lesson still catches anything: score with trend glyph, last hit, and the **last-16-times-it-surfaced** strip (caught / surfaced-still-missed / no-outcome) with its honest empty — *"No outcomes recorded. Nothing links this lesson to a check, so a hit can't be told apart from noise."* Near-duplicates resolve by *"Keep one · the rest retire into it"*. The page header states the split from Memory: unpublished candidates live there, published lessons here.

## S10 — Observe: Activity, rebuilt

Six lenses — everything, agent writes, human decisions, keys & access, memory changes, rejected — as tiles with counts and hints. A 48-bucket, 30-minute histogram stacked agent / human / rejected, clickable to filter, with a clearable bucket chip and a 1h / 24h / 7d / 30d range. Person / surface / object facets. Group by time or by target. Rows: time, actor avatar, action, target, summary, the `actor via agent` line, surface, and an ok/rejected verdict chip. An event panel with the actor chain (person to key to agent), the field-level diff, event/time/surface/trace metadata, and three pivots: all events on this target, everything by this actor, copy trace id. `J/K` moves. Export-view to CSV. An OTLP status strip that links to S15.

Backend: the events endpoint gains facet filters, bucket aggregation, the stored field diff and the trace id. **Rejected mutations must be readable** — the whole lens set is worthless if only accepted writes are recorded, and that needs checking before this slice is sized.

## S11 — Observe: Live

The presence strip: one chip per agent, role-coloured, with what it is doing and how long ago, click to focus. The **planner desk**: per planner, its allocations as worker to tickets, and its recent decisions verb-tagged allocate / hold / re-task / split, plus a held-back panel naming the colliding area and who holds it. **Tickets in motion**: one lane per ticket over a ten-minute window, dots typed read / write / review / reported / planner / refused, and the last event per lane. A **stream** with all / writes / reads / planner / failures filters that honours the focused agent. Pause and resume, and the KPI row: online, touches per minute, planner calls, refused in the last ten minutes.

## S12 — Observe: Harness — three tabs

- **Performance** (mostly built): the grid, the effort bands, the below-floor grey with its reason, the cell detail with its weekly trend and *"what gbfleet is told"*, and the **effort curve** — signed-off rate as items get bigger, above-floor cells only.
- **Guidance** (new): the routing table actually served to gbfleet, per function and effort band, with verdict, confidence, pick, evidence and fallback; its generation stamp (window, floor, attempts, supervisors served); the **grading rules** that turn a cell into a verdict; and **As served** — the verbatim `fleet_status` text, with *"Nothing here is hidden from the agents."*
- **Changes & probes**: recommendations as drafts with their framing — *"Accepting one records that you have seen it… This page changes nothing"* — the replay evidence, accept and dismiss, and the existing probe panel with its per-attempt token estimate.

## S13 — Feedback Kit: customize, and make the widget reachable

- **Customize**: embed mode, launcher label, theme, accent, corner radius, enabled types, every copy field, the options checklist, the Turnstile note, the embed snippet with its ingest token, and a live preview in test mode that walks through to the thank-you state.
- **Setup** (new): *"This deployment runs on a private host, so people on your website can't send to it directly."* Three routes — relay, custom domain, none — with their tags; for a custom domain, the DNS record table with per-record status and a Check-DNS action plus the automatic-TLS note; numbered steps with their commands and results; a readiness checklist; and the *Why the cloud org?* explanation — *"It's the only public piece. It holds submissions for up to 7 days and never sees your code or memory"* — with its link to Sync link settings.

## S14 — Usage: the deployment-wide page

A new route in the nav footer. The deployment identity strip (self-host or hosted, host, version, license). Five KPIs with sparklines and deltas: MCP calls, agent sessions, active projects, seats in use, memory shards. A stacked per-day MCP-call chart with a per-project legend that toggles series, over 7d / 30d / 90d, bucketed above 45 points. A by-project table (calls bar, agents, shards, done) with sort. License limits with per-limit bars and the on-pace note. Model usage by harness and model — spawns, tokens, estimated cost, `$0` for local models — with the caveat that cost is estimated from list prices of the tokens agents reported. Busiest API keys with owner, calls and last-seen. CSV export.

Backend: one usage aggregate endpoint. Per D5, a limit the license does not declare is shown as undeclared, never as a computed percentage.

## S15 — Log export: OTLP settings

A new Settings panel. On/off with a status strip (last batch, sent 24h, dropped 24h, queue depth) and the paused note that events are still kept locally for 90 days either way. Collector configuration: endpoint, gRPC against HTTP/protobuf, gzip against none, editable headers, and Send-test-batch whose failure is specific — *"No port in endpoint — collector unreachable"* — and whose success reports records accepted and latency. What to send: activity events as logs, MCP tool calls as traces, agent heartbeats as metrics, plus event-type filters. Redaction: drop free-text summaries, drop client IPs, mask API keys. A live sample record that reflects the redaction choices.

Backend: export configuration, the exporter itself, and the counters the status strip reads.

## S16 — Org link: the cloud-org dialog

The nav footer's Organization entry, badged when this deployment is not linked, opens a three-step dialog: **plan** (monthly or annual, four tiers, and the note that seats are people — agents and API keys are not seats), **account** (name, work email with inline validation, org name, an auto-derived slug with the URL it produces, GitHub SSO), and **review and link** (the summary, and a *what gets linked* list that states plainly that code, memory shards and item content stay on this box and the connection is outbound only). Already-have-an-account jumps to the link step. Enterprise routes to sales instead of creating anything.

## S17 — Project Home and Sync/Link scoping

- **Project Home**: the counts strip (items, in flight, PRDs, memory shards, graph nodes, agents live), the no-code-graph warning, the Work card grid, the Agents-and-memory card grid, and cross-project dependencies — depends-on and depended-on-by, stale edges dashed, with both honest empties (*"Nothing internal — every dependency resolved to an external package"*, *"No sibling repo declares this one"*). Plus the loading and error states it lacks.
- **Sync/Link**: the mapped-projects table (link state, graph-sync state, last sync, attach and detach) and the **project scope bar** that gates the panels below it, so code-graph privacy, push and portable-bundle controls are visibly per-project rather than ambiguously global.

---

## Acceptance criteria

<!-- framing -->

- **A1** Every surface in §2.1 has a test that fails the fetch and asserts the error affordance, and a test that asserts the empty-state title is *not* rendered on failure.
- **A2** Every destination in the designs is reachable from the rail or footer in both hosted and self-host mode, and the Observe rail is identical in both.
- **A3** `web/src/__tests__/chrome-font-sans.test.ts` still passes after every slice — no slice adopts the design's uppercase-mono chrome.
- **A4** Activity can be filtered to rejected mutations and shows at least one, against a seeded database that contains a refusal.
- **A5** Usage renders from a single aggregate response, and shows an undeclared-limit row rather than a computed percentage when the license declares no cap.
- **A6** Log export's test batch reports a specific failure for a portless endpoint and a records/latency result for a reachable one.
- **A7** The `STATE` chip strip appears nowhere in the built application.
- **A8** `scripts/design-capture/run.sh` re-runs clean, its render check passes, and the red absence-reads-as-clean flags it raises today are gone. `pages/build-harnesses-proposed` is not overwritten.

## Phasing

S1, then S2. Then S3–S13 and S16–S17 in parallel — frontend only, clustered by feature directory so two agents never share a file. S10, S14 and S15 each pair a backend slice with a frontend slice and are sequenced within themselves; all three are gated on the reach check in §2.2 and the risks below.

## Risks and open questions

- **Scope.** Twenty surfaces is the largest single arc in this repository. The mitigation is that S1 and S2 are cheap and remove the defect class, and everything after is independently shippable and independently valuable.
- **Collision with GRPH-P46.** P46 is approved and touches `index.css`, `components/ui/*` and the shell. Slices here touch `features/*`. Any slice that needs a token or a primitive change asks P46 for it rather than editing those files, and a PR in this arc that edits them is a review bounce.
- **Backend reach, unverified.** S10 assumes the event record can yield a field diff and a trace id, and that refusals are recorded at all. S14 assumes attempt-record token counts are complete enough to price. Neither is measured yet, and neither slice can be sized before they are.
- **Open: the seat cap.** Does the self-host license declare one? D5's undeclared-limit row depends on the answer.
- **Open: OTLP scope.** Per-deployment or per-project? The design files Log export under *project* settings but describe a deployment-wide collector.
- **Open: relay hosting.** S13's relay route assumes a cloud-org-hosted ingest endpoint. Does that exist today, or is the route a design for something unbuilt?
- **Open: how many more design divergences.** §1.3 found one (GRPH-938 casing) by checking a shipped test. The design set was authored before the last three chrome decisions landed; the rest of it has not been audited against them.
