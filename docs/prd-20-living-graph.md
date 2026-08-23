# PRD-20 — The living graph: an interactive code graph with agent presence

**Ledger id:** GRPH-P20
**Status:** approved — reached by completing the grill (PRD-15), 2026-08-14. Decisions in §8.
**Depends on:** PRD-17 (fleet roles, `Agent`, `AreaReservation`) · AL-192 (collision clustering) · AL-197 (principal on events)
**Blocked on:** GRPH-380 (the all-in-one posture claims through the collision divvy) — D4–D7 read a table that is empty until it lands
**Complemented by:** GRPH-381 (a node kind for docs/config) · GRPH-382 (`CodeNode.kind` is 97% one value)
**Touches:** `web/src/features/code/CodeGraphView.tsx` · `web/src/features/links/LinksGraphView.tsx` · `backend/app/services/code_graph.py` · `backend/app/services/fleet.py`

## 1. Overview

Graphban already has the thing the industry is currently discovering it wants. This PRD is
not about building a context graph — it is about the fact that ours is **rendered as a
poster when it should be an instrument**, and that the single most valuable fact we hold
about it, *who is inside it right now*, is on a different screen.

### 1.1 The prompt: what the K3 article claims, and where we actually stand

An article making the rounds ([Ricker, *Context Graph Engineering With K3*][article],
390K views; amplified by [@antpalkin][post], 326K views) argues that swarms of agents
produce a **pile**, and that the value was never in the individual findings but in how they
connect. Its claims, and our position against each:

| The article's claim | Graphban today |
| --- | --- |
| Nodes are what agents find | ✅ `describe_code` upserts module/file/symbol nodes as agents work |
| Edges are how they relate | ✅ **Typed** — `imports`/`calls`/`owns`/`tested_by`/`references`, plus `dependency`/`code`/`semantic`/`tag` on the links graph |
| "Two agents touch the same entity → draw an edge" | ✅ `clustering.shared_touchpoints` + `collision.py` — and we use it to *prevent* collisions, not just to observe them |
| Explainable — every edge traces to its shared source | ✅ **Better.** `GraphLink` carries `reason` *and* `confidence` (0–1); the article's edges carry neither |
| It compounds — next launch adds to the same graph | ✅ Upsert semantics + a `fresh` flag that marks a node stale rather than silently trusting it |
| Ask the graph structural questions | ❌ **We compute none of them** (§3.2) |
| One connected map you can actually use | ❌ **It is a 900×560 fixed-viewBox SVG you cannot pan, zoom, drag or search** (§3.1) |

The honest summary: **our graph is substantially better-typed and better-sourced than the
one the article is selling, and substantially worse to look at.** We built the hard half and
shipped the easy half as a diagram. Two rows in that table are the whole of this PRD.

And we hold one thing the article has no concept of. K3's graph is a record of what agents
*found* — inert the moment the run ends. Ours is a record of a codebase that **agents are
still editing**, and PRD-17 gave us first-class `Agent` rows, live `AreaReservation` leases,
and a presence clock. A graph that knows which of its nodes are under someone's hands right
now is not a research artifact. It is a **status board for a running fleet**, and nothing in
the article's framing reaches it.

[article]: https://x.com/0xRicker/status/2087163793558126997
[post]: https://x.com/antpalkin/status/2087210112716787915

### 1.2 What this is not

- **Not a graph library adoption.** The layout stays ours (D1). We are fixing that it is
  *duplicated* and *synchronous*, not that it is hand-written.
- **Not a new data model.** Every signal this PRD renders already exists in the database.
  There is no new table and no new write path — the presence design (D4–D7) is a join, not a
  schema.
- **Not a re-theme.** The token set in `index.css` is correct and stays. The "visual refresh"
  here is the graph learning to carry information it already has, not new colors.
- **Not the links graph only, or the code graph only.** They are the same widget rendered
  twice from two near-identical copies of one function; this PRD makes that literally true.

### 1.3 The load-bearing invariant

**A node glows because an agent holds a live lease on it, never because an item declares an
intention.**

This is the corrected form. The first draft said "holds a lease … never because an agent
*intends to touch it*", and rejected `Item.touchpoints` as declarative — while specifying a
source that carries the same guesses. `claim_cluster` reserves `cluster["areas"]`, which
`collision.touch_areas()` fills from an item's touchpoints **or, when it has none, from
`collision.predict_areas()`**. The cluster dict already carries `predicted: bool` to say which
happened, and the draft dropped it. Prediction is not excluded by the source; it is *labelled*
by it, and D5 renders a predicted area differently from an actual one.

What the lease genuinely buys is expiry. `AreaReservation` carries `expires_at` on the shared
lease clock, so a dead agent's glow fades **by construction** — no sweeper, no reaper, no new
failure mode. This is the same reasoning `fleet.active_reservations` already documents for
itself: a stopped sweeper would freeze the divvy with every cluster looking permanently taken
and no error anywhere to explain it. The visual layer inherits that property for free by
reading the same function.

Two consequences the draft did not state, both load-bearing:

- **Reservation lifetime and presence TTL disagree.** A reservation expires at
  `now + lease_seconds` (600s), while presence TTL is `lease // 4` (150s). An agent therefore
  reads *offline* on the Fleet roster for up to 450s while still glowing here. The lease is
  genuinely still held, so the glow is correct — but the two screens must not silently
  contradict each other, which is why D5 shows time-remaining rather than only a holder.
- **Only one code path writes reservations at all.** `claim_cluster` (`fleet.py:1049`) is the
  sole writer; `claim_next` and `next_cluster` write none, and the all-in-one prompt — the
  default posture — says `claim_next`. Until GRPH-380 lands, everything in D4–D7 reads an
  empty table on a default install.

The corollary is what makes the feature worth building: **when the glow is wrong, the fleet
is wrong.** A stale glow is a leaked reservation. Two clouds on one node is a partition
violation (D6). The graph stops being decoration and becomes an assertion about fleet
state that a human can falsify at a glance.

## 2. Goals

- **G1** — The graph is navigable at the scale we actually reach: pan, zoom, drag, find-a-node.
- **G2** — Layout has **one owner**, shared by both graph views, and never blocks the main thread.
- **G3** — A node under an agent's active reservation is visibly held, and stops being held
  when the lease lapses — with no new sweeper.
- **G4** — Whose agent holds it is legible without a click, using the per-user color we
  already store, on a visual channel that does not collide with node kind or edge type.
- **G5** — The graph answers the three structural questions the article names — hub, cluster,
  path — because we already hold the data to answer them.
- **G6** — The graph is operable without a mouse and legible to a screen reader.
- **G7** — No new tables, no new write paths, no new color vocabulary.
- **G8** — Held work the graph cannot place is **reported, never dropped**. An empty region
  means "nobody is working here", not "we could not resolve it".
- **G9** — The graph stays legible above the detail budget, by changing what it draws rather
  than by drawing the same thing slower.

## 3. Problem (what is actually wrong today)

### 3.1 The interaction surface (verified against the tree)

Both `CodeGraphView.tsx` and `LinksGraphView.tsx` render `<svg viewBox="0 0 900 560"
className="h-full w-full">`. Consequences, in descending order of how fast a user hits them:

- **No pan, no zoom, no drag.** The viewBox is a constant. On a wide monitor the graph is
  scaled up and still shows the same fixed frame; on a dense project the nodes converge into
  the middle and there is no way to get closer.
- **Labels are unconditional and uncollided.** Every node emits `<text x={11} y={4}>` with
  no level-of-detail rule and no overlap avoidance. Past roughly 40 nodes the labels are the
  densest ink on the screen and the least readable.
- **Layout recomputes on every filter toggle.** `computeLayout` is a `useMemo` keyed on
  `[ids, edges]`, and `edges` is itself a `useMemo` filtered by the enabled edge types. So
  toggling a chip re-runs 300 iterations of O(n²) pairwise repulsion **synchronously on the
  main thread** — and worse, the graph *rearranges*, so a filter chip does not filter a
  picture, it replaces it. The user loses their place every time they ask a question.
- **Selection is one hop.** `hl` collects the selected node, its edges, and their other
  endpoints. There is no way to expand to two hops, to isolate a subgraph, or to pin.
- **No hover state.** The only feedback that a node is interactive is `cursor-pointer`.
- **Not accessible.** Clickable `<g>` elements with no `role`, no `tabIndex`, no
  `aria-label`. The graph is unreachable by keyboard and silent to a screen reader.
- **Never refetches.** `useCodeMap` has no `refetchInterval` — unlike `useFleet`, which
  polls at 15s precisely because presence decays with time. An agent describing code right
  now does not appear until the user navigates away and back.

### 3.2 The graph cannot be asked anything

The article's central demonstration is three queries: *which node is the single point of
failure* (most inbound dependency edges), *which things move together* (densest cluster),
*what connects these two* (shortest path through shared entities).

We hold typed, directed edges — strictly more than K3 needs for all three — and we compute
**none** of them. `NodeInspector` lists a node's outgoing and incoming edges and the items
touching it. That is a 1-hop adjacency dump. There is no degree ranking, no component or
community detection, no path finding. `code_graph.neighbors()` is the whole of our graph
query surface.

This is the cheapest win in the PRD. Inbound-edge ranking is a `Counter` over `list_edges`.

### 3.3 One function, two copies

`computeLayout` in `CodeGraphView.tsx:44-98` and `LinksGraphView.tsx:25-82` are the same
function — same circle seed, same `REST = 150`, same `26000 / d²` repulsion, same 300
iterations, same `0.02` center pull, same `±14` step clamp — differing only in whether the
edge endpoints are named `src`/`dst` or `a`/`b`.

Our own design philosophy opens with *"one owner per fact"* and the observation that **a weak
pattern left in the tree becomes prompt material for the next agent**. This is that, sitting
in the two files most likely to be copied when someone adds a third graph. Extracting it is
not tidying; it is the precondition for G2 — there is no point moving layout to a worker
twice.

### 3.4 The graph is 30% of the codebase, and does not say so

Measured on the live instance, 2026-08-14: **123 `CodeNode` rows against 401 tracked
`.py`/`.ts`/`.tsx` files.** Of 100 touchpoints on live backlog items, **15 resolve to no node
at all** — `docs/mcp.md`, `AGENTS.md`, `README.md`, `web/nginx.conf`, `.cursor/rules/*`, and
areas that are not paths in the first place (`vercel env`, `twitch developer console`,
`../ascme-labs/**`).

This is not a footnote; it is the constraint that shapes D4. A presence layer that renders
only what it can resolve would have shown GRPH-380's agent working in three files and been
silently blind to `web/src/features/fleet/FleetView.tsx` — the file where most of that item's
change actually lands. G8 exists because of this measurement.

## D1 — One layout owner, off the main thread

Extract to `web/src/lib/graph/layout.ts`, generic over an edge shape:

```ts
export interface LayoutEdge { a: string; b: string }
export interface LayoutOpts { width: number; height: number; iterations?: number }
export function computeLayout(ids: string[], edges: LayoutEdge[], o: LayoutOpts): Record<string, Pos>
```

Both views adapt their edges (`{a: e.src, b: e.dst}`) at the call site. The function stays
deterministic — no `Math.random()` — because stability across renders is a property we
already rely on and would be missed immediately.

Run it in a Web Worker behind a `useGraphLayout` hook. The hook returns the previous positions
while a new run is in flight, so the graph never blanks. This kills the toggle-jank in §3.1.

**One worker module, one instance per view.** Code and Links are separate routes and never
co-render, so a shared instance buys nothing and costs request tagging plus a race between two
clients of one worker. The instance is torn down on unmount. Revisit only if the two views ever
appear on screen together.

**Positions survive edge filtering.** Layout is computed from the *unfiltered* edge set and
memoized on `ids` alone; the enabled-type chips change which edges are *drawn*, never where
nodes sit. Filtering becomes what a user expects — the same map with fewer lines — instead of
a reshuffle. This is a one-line change to the memo key and it is the single largest usability
win in this section.

**A manual re-layout escape hatch.** Under a deep filter, positions reflect edges the user can
no longer see. A "re-layout to visible" control appears **only while a filter is active** and
recomputes from the filtered edge set, preserving pins. It is never automatic: automatic
re-layout is exactly the jank this section removes.

**Budget.** 300 iterations of O(n²) is roughly 37M pair-interactions at 500 nodes and 96M at
800 — seconds, not milliseconds. The detail view is supported to **800 nodes** (the live graph
is 123; full describe coverage lands near 400, so this is real headroom) with **first layout
≤1.5s** and **no main-thread block over 16ms**. Above the budget, D9 changes what is drawn.

## D2 — Pan, zoom, drag, find

- **Pan/zoom** — a `<g transform>` driven by wheel + drag, clamped to 0.25×–6×. Zoom to
  cursor, not to center. Double-click empty space resets.
- **Drag a node** — pins it (`pinned: Set<string>`); pinned nodes are excluded from the
  integrate step and rendered with a subtle ring. Drag is how a human says *"this one matters,
  stop moving it"*, and pinning is the only honest way to honor that.
- **Find** — a `/`-focused filter box. Matching nodes keep full opacity, the rest drop to the
  existing dim value; the viewport eases to fit the matches. Reuses the `hl` mechanism, so
  search is highlight-by-another-name and not a second visual language. **Find searches every
  node, including those inside collapsed components in D9**, and selecting a match inside a
  collapsed component enters that component. A search that cannot see what the view is hiding
  is worse than no search.
- **Level of detail** — labels render only when `zoom > 0.8`, for the selected/highlighted
  set, for search matches, or for the top-N nodes by degree. At default zoom on a large graph
  you see structure and the hubs are named; zoom in and the names arrive.

## D3 — Hover, focus, and reach

- **Hover** — the node grows by 2px and its 1-hop neighborhood lifts to full opacity without
  committing a selection. Read-before-click, which is what makes a dense graph explorable.
- **Two-hop expand** — `⇧`-click, or an "expand" affordance on the inspector, widens the
  highlight set by one ring. The `hl` computation becomes a BFS to depth `d` (default 1).
- **Keyboard and a11y** (G6) — the `<svg>` gets `role="application"` and an `aria-label`
  naming the counts. Nodes become focusable (`tabIndex={0}`, `role="button"`,
  `aria-label="{kind} {path}, {n} connections, held by {agent}"`), `Tab` walks them in degree
  order, `Enter` selects, `Esc` clears. Arrow keys pan. A focused node gets a visible ring —
  the same ring as selection, so there is one focus vocabulary.

## D4 — The held set

New read-only endpoint, `GET /api/fleet/presence?project_id=…`, backed by a new function in
`services/fleet.py` that composes what already exists:

```python
def held_areas(db, project_id) -> dict:
    """Live reservations, resolved to the agent and the human behind it.

    Reads `active_reservations` so the lease clock governs the glow: nothing here needs
    sweeping, and an agent that died stops holding by the same lapse that already frees
    its items.
    """
```

**Payload.**

```
{ served_at, heartbeat_interval_seconds,
  held:    [{ area, node_paths[], predicted, agent_id, agent_key, agent_label, active_role,
              state, item_id, expires_at, user_id, user_initials, user_color }],
  off_map: [{ area, reason: "undescribed" | "stale", ...same holder fields }],
  truncated, total }
```

`node_paths` is **plural** because one area measurably resolves to many nodes. `predicted`
carries `cluster["predicted"]` through from the divvy, per §1.3. `served_at` lets the client
compute time-remaining without assuming clock sync. `heartbeat_interval_seconds` is echoed so
the poll cadence needs no second call.

**Off-map areas are reported, never dropped (G8).** An area that resolves to no `CodeNode`
appears in `off_map` with a reason — `undescribed` (no node exists) or `stale` (the node is
`fresh=False`; `prune` marks rather than deletes, `code_graph.py:118`). The UI renders these in
a tray with the raw area text and the holder, plus a count. A presence payload that silently
omits what it could not resolve is an absence reading as a clean result, and it is the failure
most likely to make someone trust the graph wrongly. The count doubles as a visible measure of
`describe_code` coverage debt, which is 70% of source files today and currently invisible.

**No server-side classification of off-map areas.** `AGENTS.md` (a real repo path, merely
undescribed) and `vercel env` (never a path) are different things, but the server cannot tell
them apart from the string, and heuristics on slashes or extensions misfile `web/nginx.conf`
and `../ascme-labs/**` in both directions. `git ls-files` is the only authority and the hosted
backend does not have it. All unresolved areas therefore share one tray, and describing a file
quietly promotes it onto the canvas.

`CodeRef` is the curation route for anyone who wants the association to be explicit rather than
inferred: it is "stored by path so it can point at a not-yet-described node"
(`models/__init__.py:1163`), and `link_code` accepts undescribed paths today.

**Resolving an area to nodes — a matcher of its own.** Presence uses
`area_matches(area, path)` = exact match, **or** glob, **or** area-is-a-directory-prefix-of-path.
It explicitly does **not** reuse `clustering._match`'s shared-parent-directory rule, which
measurably turns one file area into 25 nodes (and `web/src/features/*` into 41). Over-matching
is *safe* for the collision divvy — you over-block, you never collide — and is a *lie* for
presence, which would claim an agent is somewhere it is not. Same vocabulary, opposite failure
preferences. The earlier claim that sharing `_match` means the divvy and the graph "cannot
drift" was backwards and is withdrawn. `area_matches` is `::`-aware from the start so a symbol
node matches its file's area once GRPH-382 lands.

**Poll cadence.** The client polls at `heartbeat_interval_seconds`, already in the fleet
payload, rather than a hardcoded number. Presence is only as fresh as the heartbeat that
feeds it, and asking faster than agents report renders a confidence we do not have.

**No pagination.** A hard cap on reservations scanned, with `truncated` and `total` returned.
Paginating a live viewport snapshot is meaningless — you would render half a fleet and it would
look complete.

**Authorization: JWT only, via `authz.require_readable`.** Not reachable with an agent API key.
Agents do not need to know which human is editing what — D8's `graph_query` covers their needs.
Presence is a surveillance surface with a person's name attached, and `test_cross_tenant.py`
exists because this codebase has shipped isolation bugs before. Restricting it to human sessions
removes the class rather than guarding it.

## D5 — How a held node looks

Three visual channels, strictly separated, because the graph has already spent two of them:

| Channel | Encodes | Vocabulary |
| --- | --- | --- |
| Node **fill / stroke** | *What kind of thing is this* | `module` lime · `file` blue · `symbol` purple; dashed = undescribed or stale |
| Edge **stroke** | *What kind of relation* | the five `EDGE_META` colors |
| **Cloud** (new) | *Whose agent is holding it* | `User.avatar`, per human |

**Caveat on the first channel.** The live graph is 119 `module`, 4 `file`, 0 `symbol` — 97% one
value — so today the kind channel carries almost no information and the separation argument
above defends something that is not yet there. GRPH-382 fixes the population; this design is
correct once it does.

The cloud is a blurred radial fill on a layer **beneath** the edges, at ~14–18% opacity with
a soft `feGaussianBlur`, radius scaled to the number of held nodes it covers. It never
touches the node's own fill or stroke. This is the load-bearing rule of the visual design:
**a held node must still say what kind of node it is.** Tinting the node itself would overload
the one channel that already carries meaning, and a user would lose the ability to tell a
held symbol from a module at a glance — which is precisely when they most need it.

On top of the cloud, at the node:

- A **soft pulse ring** in the user's color — `animation` on `r` and opacity, ~2.4s, matching
  the existing `.blink` cadence so the app has one idle rhythm.
- A **role dot**: the PRD-17 `ROLE_TONE` color (planner purple, worker lime, reviewer amber),
  because *what they are doing to it* is a different question from *whose they are*.
- The agent key (`GRPH-A3`) on hover and in the inspector, never persistently — it is the
  third label on a surface that already has too many.

**Predicted areas read differently.** When `predicted` is true the cloud edge is dashed rather
than soft. The claim is real — the lease is held — but *where* it lands is a guess from
`collision.predict_areas()`, and §1.3 requires that be visible rather than smoothed away.

**Time-remaining, not just a holder.** The inspector shows how long the reservation has left,
computed against `served_at`. This is what stops the graph and the Fleet roster from silently
contradicting each other during the 450s window where an agent reads offline but still holds.

**Reduced motion.** `prefers-reduced-motion` replaces the pulse with a static ring at the
pulse's mean opacity. The information is in the ring, not the animation; the animation only
makes it findable in peripheral vision.

## D6 — Overlapping clouds are the alarm

When two agents belonging to **different humans** hold areas that resolve to the same node,
their clouds overlap and the blend is visibly a third color.

This is not a rendering edge case to be smoothed away — it is the most valuable pixel in the
feature. `collision.py` exists to partition work so this cannot happen.

**How it can actually happen.** The first draft listed causes that do not exist — an
`all-in-one` agent "running unpartitioned" creates no reservations at all, and `claim_cluster`
already refuses any cluster colliding with another agent's areas (`fleet.py:1036`), so overlap
cannot arise through the only writing path. The reachable causes are narrower and all worth
knowing about:

- **A lease-lapse race** — one agent's reservation expires mid-edit and another claims the area
  while the first is still working.
- **A matcher gap** — two areas that `areas_collide` does not consider colliding but that
  `area_matches` resolves onto a shared node.
- **A claim made outside the divvy** — anything that took an item without going through
  `claim_cluster`.

Every one of those is something a person needs to know now, and none of them raises an error
anywhere today.

So the overlap gets promoted rather than blended into mush: the node takes a **contention
ring** in `--color-st-blocked`, and the inspector leads with *"held by 2 agents across 2
users"* naming both. The clouds still render underneath — the color blend is what draws the
eye, the ring is what confirms it.

**The ring is binary.** Contended or not; three or more users do not get a thicker or segmented
ring. The count lives in the inspector and the legend — *"held by 4 agents across 3 users"*.
Encoding N in the ring would add a fourth visual channel to a surface that argues carefully for
three, and the alarm is "more than one user" — the exact number is read, not glanced.

Two agents from the **same** human overlapping is ordinary (one person, two windows, one
worktree) and gets no ring — same color, one cloud, denser. The alarm is keyed on *distinct
users*, because that is the case where nobody involved can see the other's terminal.

## D7 — The fleet legend

A collapsible strip along the bottom of the graph: one chip per human with live agents —
`Avatar` in their color, name, agent count, held-node count. Clicking a chip **solos** that
user: their clouds stay, everyone else's fade, and the held nodes highlight through the
existing `hl` path. This answers *"what is my team doing to the codebase right now"* without
a click into any node, and it is the screen we do not currently have anywhere.

The legend also carries the **off-map count** from D4 — *"3 held areas not on this map"* —
opening the tray. That number belongs next to the fleet it describes, not buried in an
inspector.

## D8 — Asking the graph (G5)

Three functions in `services/code_graph.py`, each exposed on `GET /api/agent/code/analysis` and as
one MCP tool (`graph_query`) so agents get them too — an agent about to refactor should be
able to ask what depends on it:

- **`hubs(db, project_id, *, edge_types=None, limit=10)`** — nodes ranked by inbound degree.
  The article's "single point of failure", and a `Counter` over `list_edges`. Rendered as a
  **Hubs** panel; hovering a row rings that node on the graph. Node radius optionally scales
  with inbound degree, so hubs are visible without opening a panel. Also supplies the anchor
  selection for D9.
- **`components(db, project_id)`** — **connected components only**, over the enabled edge
  types. Deterministic, O(V+E), and adequate at this graph size. Modularity is explicitly
  **out** for v1: every practical method (Louvain, Leiden) is stochastic and order-dependent,
  which contradicts the reason §7 gives for rejecting d3-force and cytoscape. If it is wanted
  later it must name Leiden, a fixed seed, and a documented node ordering.
  Rendered as a faint **convex hull with padding** — not an alpha shape — behind each
  component, in `--color-line-3`, a *structural* channel distinct from the presence clouds by
  both color and blur radius. A hull is drawn only for components of **three or more** nodes;
  two nodes is just an edge.
- **`path(db, project_id, a, b)`** — shortest path over the **undirected** projection of the
  enabled edge types, returning the edges with their types and traversed direction. The
  article's "what connects these two". Rendered by dimming everything not on the path.
  Reachable from the UI by `⌥`-clicking a second node.

Each is a graph read over data we already have. None needs a new table, a background job, or
a write path.

## D9 — The galaxy view (scale)

Above D1's detail budget, the graph **changes what it draws rather than drawing the same thing
slower** (G9). Components collapse into super-nodes, each labelled by its highest-degree member
from `hubs()`, with aggregated edges between them. Presence renders as one hull per
(user, component) rather than one cloud per node. Filters and zoom enter a component, which
lays out only that component's nodes at full detail.

The point is what this does to the bound: layout cost goes from O(n²) over the whole repo to
O(k²) over k components plus O(m²) inside the entered one, so **the ceiling becomes the size of
the largest component, not the size of the repository**. It also composes with D8's determinism
— component detection is deterministic, so anchors and grouping are stable across renders, the
same property D1 protects for node positions.

Both halves of this already existed in the draft, unconnected: the per-(user, component) hull
was filed under risks as a cloud-cost mitigation, and `hubs()` was specified for a side panel.
This section is what joins them.

Requirements the flat view did not have: a **"you are here" affordance** so semantic zoom does
not lose the user, and D2's find must search across collapsed components and enter the one
holding a match. This is a second rendering mode and ships in its own phase — it must not
silently widen D1 or D2.

## 4. Data model

**None.** That is the point of D4. Stated explicitly so a reader does not go looking:

| Signal | Existing source |
| --- | --- |
| Which node is held | `AreaReservation.area` × `area_matches` (new function, no new data) |
| Whether the area was guessed | `cluster["predicted"]`, already returned by `claim_cluster` |
| Until when | `AreaReservation.expires_at`, the shared lease clock |
| By which agent | `AreaReservation.agent_id → Agent` |
| Doing what | `Agent.active_role`, `presence_state()` |
| For which human | `Agent.api_key_id → ApiKey.user_id → User` |
| In what color | `User.avatar` — already a hex color |
| Under what initials | `User.initials` |
| How often to ask | `fleet.heartbeat_interval_seconds()` |
| Held but unplaceable | derived per read — no stored row, so it can never go stale |

## 5. Acceptance criteria

**Interaction**
1. Wheel zooms to cursor within 0.25×–6×; drag pans; double-click on empty space resets.
2. Dragging a node pins it; pinned nodes do not move on subsequent layout runs.
3. Toggling an edge-type chip changes which edges are drawn and **does not move any node**.
4. At 800 nodes, first layout completes in **≤1.5s** and no main-thread task exceeds **16ms**;
   previous positions stay on screen while a new run is in flight. (Latency is the bound — "the
   main thread is not blocked" is satisfied by any worker and asserts nothing.)
5. `computeLayout` exists in exactly one file: `grep -rl "26000" web/src | wc -l` returns 1
   (it returns 2 today — `CodeGraphView.tsx:66` and `LinksGraphView.tsx:48`).
6. `/` focuses find; matches stay lit, the rest dim, the viewport eases to fit. A match inside a
   collapsed component enters that component.
7. Every node is reachable by `Tab`, selectable by `Enter`, and carries an `aria-label`
   naming its kind, path, connection count, and holder.
8. The re-layout control is absent with no filter active, present with one, and preserves pins.

**Presence**
9. An agent taking a reservation on `services/items.py` makes that node glow in its owner's
   `User.avatar` color within one heartbeat interval.
10. When the reservation lapses, the glow disappears **without any sweeper running** — proven
    by a test that advances the clock via the existing `now=` injection on
    `active_reservations` (`fleet.py:947`) and asserts the presence payload empties.
11. A held node still renders its kind color and its described/stale stroke unchanged.
12. A reservation on `services/items.py` resolves to **that node only** — not to its directory
    siblings. A test pins the count, because `_match` returns 25 for this input today.
13. An area matching no node appears in `off_map` with `reason: "undescribed"`, is counted in
    the legend, and is listed in the tray with its holder. It is never omitted.
14. A reservation whose `predicted` flag is true renders a dashed cloud edge; an actual area
    renders a soft one.
15. Two agents from **different** users on one node produce a contention ring and an
    inspector line naming both; two agents from the **same** user produce neither; three users
    produce the same ring and the count "across 3 users".
16. `prefers-reduced-motion` yields a static ring, no animation.
17. Clicking a legend chip solos that user's clouds and highlights their held nodes.
18. `GET /api/fleet/presence` returns 401 for an agent API key and succeeds for a project
    member's JWT; a non-member receives no `user_color` or `user_initials` for anyone.

**Query**
19. `hubs()` returns the node with the most inbound edges, and the Hubs panel ranks it first.
20. `components()` returns identical output across repeated calls on unchanged data; hulls are
    drawn for components of ≥3 nodes and omitted below that.
21. `path(a, b)` returns a typed edge list, and the graph dims everything off the path.
22. `graph_query` is callable over MCP and appears in the tool count that `test_docs_sync.py`
    ratchets.

## 6. Phasing

| Phase | Contents | Why this order |
| --- | --- | --- |
| **P1** | D1 (extract + worker + budget) + AC-3 (stable positions under filtering) | Everything else renders into this surface. Shipping presence onto a graph that reshuffles on every toggle would make the glow look like a bug. |
| **P2** | D2, D3 — pan/zoom/drag/find, hover, keyboard, a11y | The interaction floor. Independently valuable, and the only part that helps at today's node counts. |
| **P3** | D8 — hubs, components, path | Fully independent of presence; can land any time after P1. Moved earlier than the draft had it because D9 needs `hubs()` and `components()`. |
| **P4** | D4, D5 — the held set, the clouds, the off-map tray | The feature. **Requires GRPH-380**, without which the reservation table is empty on a default install. Needs P1's stability to read as presence rather than churn. |
| **P5** | D6, D7 — contention ring, fleet legend | Needs P4. D6 in particular is only meaningful once clouds are trusted. |
| **P6** | D9 — the galaxy view | A second rendering mode. Needs `hubs()` and `components()` from P3, and the per-(user, component) hull only makes sense once P4 exists. |

## 7. Risks

- **Color collision between users.** `User.avatar` defaults to `#a78bfa` and users may pick
  anything. On our dark surfaces a 15%-opacity cloud is safe, but two users with near-identical
  colors are indistinguishable. Mitigation: the legend is the ground truth for *whose*, and the
  org settings page should warn on a color within a small ΔE of an existing member's — a
  one-time check at pick time, not a render-time remap. Note the default is **exactly**
  `--color-purple`, which is also the `symbol` kind color, so the default must change or every
  cloud on a fresh install is the same purple.
- **Presence honesty.** The glow is only as true as the heartbeat. An agent that dies mid-lease
  keeps its glow until `expires_at`, by design (§1.3). This is correct — the lease *is* still
  held — but it must be legible, which is why D5 shows time-remaining.
- **Coverage debt is now visible, and that will look like a regression.** The off-map tray will
  show real numbers on day one (15% of live touchpoints today). That is the feature working, not
  failing, and the phrasing in the UI has to make that unambiguous or the first reaction will be
  that presence is broken.
- **Scope creep into a graph library.** The pull toward d3-force or cytoscape will be strong
  around P2 and again at P6. The determinism requirement is the reason to resist: our layout is
  stable across renders by construction, and most libraries are not. D8's components-only
  decision is the same reasoning applied to clustering.
- **Reading cost.** `active_reservations` runs `select(AreaReservation)` with no WHERE clause
  (`fleet.py:956`), materialises every row and filters in Python. Polled per viewer at the
  heartbeat interval, plus area×node matching per poll. Bounded by the D4 cap, but the query
  wants scoping before P4 rather than after.

## 8. Decisions from grilling

Recorded 2026-08-14 across three rounds. Each entry corrects something the draft asserted.

| # | Decision | What it replaced |
| --- | --- | --- |
| 1 | The all-in-one posture claims through the collision divvy (filed as **GRPH-380**) | The draft assumed reservations exist; `claim_cluster` is the only writer and the default prompt says `claim_next`, so presence rendered nothing on a default install |
| 2 | Presence is a **live lease**, and predicted areas are labelled rather than excluded | §1.3 banned prediction while specifying a source that carries it |
| 3 | Held areas that resolve to no node are reported in `off_map`, with a tray and a count (**G8**) | Unresolvable areas were silently dropped — 15% of live touchpoints |
| 4 | Presence gets its own `area_matches`; `_match`'s shared-parent rule is not reused | The claim that sharing `_match` means the two "cannot drift" was backwards — over-matching is safe for the divvy and a lie for the graph |
| 5 | `/api/fleet/presence` is JWT-only, uncapped-but-truncating, with `held`/`off_map` split | No contract was specified at all |
| 6 | Contention ring is binary; the count lives in text | Undefined above two users |
| 7 | D6's causes rewritten | The stated causes could not occur |
| 8 | Components only, convex hulls, ≥3 nodes | "Modularity within the largest" named no algorithm and contradicted §7's determinism argument |
| 9 | AC-4 bounds latency; detail view budgeted at 800 nodes | "The main thread is not blocked" is satisfied by any worker |
| 10 | **D9, the galaxy view** — clouds and anchor nodes above the budget, filters to zoom | The draft had no scale strategy; both halves already existed unconnected |
| 11 | One worker module, one instance per view | "A Web Worker", singular, with two views mounting the hook |
| 12 | Manual re-layout escape hatch under filters | Filter-stable positions were asserted as pure win |
