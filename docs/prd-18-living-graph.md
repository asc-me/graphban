# PRD-18 — The living graph: an interactive code graph with agent presence

**Status:** draft
**Depends on:** PRD-17 (fleet roles, `Agent`, `AreaReservation`) · AL-192 (collision clustering) · AL-197 (principal on events)
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

- **Not a graph library adoption.** The layout stays ours (§4.1). We are fixing that it is
  *duplicated* and *synchronous*, not that it is hand-written.
- **Not a new data model.** Every signal this PRD renders already exists in the database.
  There is no new table and no new write path — §5 is a join, not a schema.
- **Not a re-theme.** The token set in `index.css` is correct and stays. The "visual refresh"
  here is the graph learning to carry information it already has, not new colors.
- **Not the links graph only, or the code graph only.** They are the same widget rendered
  twice from two near-identical copies of one function; this PRD makes that literally true.

### 1.3 The load-bearing invariant

**A node glows because an agent holds a lease on it, never because an agent intends to
touch it.**

Everything in §5 follows. The temptation is to light up nodes from `Item.touchpoints` — the
data is right there and the join is trivial. It is the wrong source, and quietly so:
touchpoints are *declarative*, they include `collision.predict_areas`' guesses for items
nobody has started, and nothing expires them. A graph lit from touchpoints shows a codebase
uniformly on fire, with an agent that died an hour ago still holding its glow.

`AreaReservation` is the right source because it is the *running* claim, and because it
carries `expires_at` on the shared lease clock. A dead agent's glow fades **by construction**
— no sweeper, no reaper, no new failure mode. This is the same reasoning
`fleet.active_reservations` already documents for itself: a stopped sweeper would freeze the
divvy with every cluster looking permanently taken and no error anywhere to explain it. The
visual layer inherits that property for free by reading the same function.

The corollary is what makes the feature worth building: **when the glow is wrong, the fleet
is wrong.** A stale glow is a leaked reservation. Two clouds on one node is a partition
violation (§5.3). The graph stops being decoration and becomes an assertion about fleet
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

## 3. What is actually wrong today

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

## 4. Design — the interactive surface

### 4.1 D1: One layout owner, off the main thread

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

**Positions survive edge filtering.** Layout is computed from the *unfiltered* edge set and
memoized on `ids` alone; the enabled-type chips change which edges are *drawn*, never where
nodes sit. Filtering becomes what a user expects — the same map with fewer lines — instead of
a reshuffle. This is a one-line change to the memo key and it is the single largest usability
win in this section.

### 4.2 D2: Pan, zoom, drag, find

- **Pan/zoom** — a `<g transform>` driven by wheel + drag, clamped to 0.25×–6×. Zoom to
  cursor, not to center. Double-click empty space resets.
- **Drag a node** — pins it (`pinned: Set<string>`); pinned nodes are excluded from the
  integrate step and rendered with a subtle ring. Drag is how a human says *"this one matters,
  stop moving it"*, and pinning is the only honest way to honor that.
- **Find** — a `/`-focused filter box. Matching nodes keep full opacity, the rest drop to the
  existing dim value; the viewport eases to fit the matches. Reuses the `hl` mechanism, so
  search is highlight-by-another-name and not a second visual language.
- **Level of detail** — labels render only when `zoom > 0.8`, for the selected/highlighted
  set, for search matches, or for the top-N nodes by degree. At default zoom on a large graph
  you see structure and the hubs are named; zoom in and the names arrive.

### 4.3 D3: Hover, focus, and reach

- **Hover** — the node grows by 2px and its 1-hop neighborhood lifts to full opacity without
  committing a selection. Read-before-click, which is what makes a dense graph explorable.
- **Two-hop expand** — `⇧`-click, or an "expand" affordance on the inspector, widens the
  highlight set by one ring. The `hl` computation becomes a BFS to depth `d` (default 1).
- **Keyboard and a11y** (G6) — the `<svg>` gets `role="application"` and an `aria-label`
  naming the counts. Nodes become focusable (`tabIndex={0}`, `role="button"`,
  `aria-label="{kind} {path}, {n} connections, held by {agent}"`), `Tab` walks them in degree
  order, `Enter` selects, `Esc` clears. Arrow keys pan. A focused node gets a visible ring —
  the same ring as selection, so there is one focus vocabulary.

## 5. Design — presence on the graph

This is the part the article has no answer to.

### 5.1 D4: The held set

New read-only endpoint, `GET /api/fleet/presence?project_id=…`, backed by a new function in
`services/fleet.py` that composes what already exists:

```python
def held_areas(db, project_id) -> list[dict]:
    """Live reservations, resolved to the agent and the human behind it.

    Reads `active_reservations` so the lease clock governs the glow: nothing here needs
    sweeping, and an agent that died stops holding by the same lapse that already frees
    its items.
    """
```

Each row: `{agent_id, agent_key, agent_label, active_role, state, item_id, area,
expires_at, user_id, user_initials, user_color}`.

`user_color` comes from `Agent.api_key_id → ApiKey.user_id → User.avatar`, which is
**already a hex color** (`models/__init__.py:74`, default `#a78bfa`) and already drives the
`Avatar` component. There is no new palette and no color assignment logic to get wrong — the
graph tints itself with the same color the user's face already has everywhere else in the
app. That is G4 and G7 together, at the cost of two joins.

**Resolving an area to nodes.** A reservation's `area` is a path, glob, or module — the same
vocabulary as `Item.touchpoints`, deliberately, per its own model comment. `code_graph._match`
already glob-matches that vocabulary against a `CodeNode.path`; the endpoint reuses it. No new
matching semantics, so "what the collision divvy thinks an agent has" and "what the graph
shows an agent has" cannot drift.

**Poll cadence.** The client polls at `heartbeat_interval_seconds`, already in the fleet
payload, rather than a hardcoded number. Presence is only as fresh as the heartbeat that
feeds it, and asking faster than agents report renders a confidence we do not have.

### 5.2 D5: How a held node looks

Three visual channels, strictly separated, because the graph has already spent two of them:

| Channel | Encodes | Vocabulary |
| --- | --- | --- |
| Node **fill / stroke** | *What kind of thing is this* | `module` lime · `file` blue · `symbol` purple; dashed = undescribed or stale |
| Edge **stroke** | *What kind of relation* | the five `EDGE_META` colors |
| **Cloud** (new) | *Whose agent is holding it* | `User.avatar`, per human |

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

**Reduced motion.** `prefers-reduced-motion` replaces the pulse with a static ring at the
pulse's mean opacity. The information is in the ring, not the animation; the animation only
makes it findable in peripheral vision.

### 5.3 D6: Overlapping clouds are the alarm

When two agents belonging to **different humans** hold areas that resolve to the same node,
their clouds overlap and the blend is visibly a third color.

This is not a rendering edge case to be smoothed away — it is the most valuable pixel in the
feature. `collision.py` exists to partition work so this cannot happen. If two clouds meet,
one of these is true: an item was claimed outside the divvy, an `all-in-one` agent is running
unpartitioned, a human overrode a queued cluster, or the partition has a bug. Every one of
those is something a person needs to know now, and none of them raises an error anywhere
today.

So the overlap gets promoted rather than blended into mush: the node takes a **contention
ring** in `--color-st-blocked`, and the inspector leads with *"held by 2 agents across 2
users"* naming both. The clouds still render underneath — the color blend is what draws the
eye, the ring is what confirms it.

Two agents from the **same** human overlapping is ordinary (one person, two windows, one
worktree) and gets no ring — same color, one cloud, denser. The alarm is keyed on *distinct
users*, because that is the case where nobody involved can see the other's terminal.

### 5.4 D7: The fleet legend

A collapsible strip along the bottom of the graph: one chip per human with live agents —
`Avatar` in their color, name, agent count, held-node count. Clicking a chip **solos** that
user: their clouds stay, everyone else's fade, and the held nodes highlight through the
existing `hl` path. This answers *"what is my team doing to the codebase right now"* without
a click into any node, and it is the screen we do not currently have anywhere.

## 6. Design — asking the graph (G5)

Three functions in `services/code_graph.py`, each exposed on `GET /api/code/analysis` and as
one MCP tool (`graph_query`) so agents get them too — an agent about to refactor should be
able to ask what depends on it:

- **`hubs(db, project_id, *, edge_types=None, limit=10)`** — nodes ranked by inbound degree.
  The article's "single point of failure", and a `Counter` over `list_edges`. Rendered as a
  **Hubs** panel; hovering a row rings that node on the graph. Node radius optionally scales
  with inbound degree, so hubs are visible without opening a panel.
- **`clusters(db, project_id)`** — connected components over the enabled edge types, then
  modularity within the largest. The article's "which things move together". Rendered as a
  faint hull behind each cluster, in `--color-line-3` — a *structural* channel, distinct from
  the presence clouds by both color and blur radius.
- **`path(db, project_id, a, b)`** — shortest path, returning the edges with their types.
  The article's "what connects these two". Rendered by dimming everything not on the path.
  Reachable from the UI by `⌥`-clicking a second node.

Each is a graph read over data we already have. None needs a new table, a background job, or
a write path.

## 7. Data model

**None.** That is the point of §5.1. Stated explicitly so a reader does not go looking:

| Signal | Existing source |
| --- | --- |
| Which node is held | `AreaReservation.area` × `code_graph._match` |
| Until when | `AreaReservation.expires_at`, the shared lease clock |
| By which agent | `AreaReservation.agent_id → Agent` |
| Doing what | `Agent.active_role`, `presence_state()` |
| For which human | `Agent.api_key_id → ApiKey.user_id → User` |
| In what color | `User.avatar` — already a hex color |
| Under what initials | `User.initials` |
| How often to ask | `fleet.heartbeat_interval_seconds()` |

## 8. Acceptance criteria

**Interaction**
1. Wheel zooms to cursor within 0.25×–6×; drag pans; double-click on empty space resets.
2. Dragging a node pins it; pinned nodes do not move on subsequent layout runs.
3. Toggling an edge-type chip changes which edges are drawn and **does not move any node**.
4. Layout runs in a worker; the main thread is not blocked for a 500-node graph, and the
   previous positions stay on screen while a new run is in flight.
5. `computeLayout` exists in exactly one file: `grep -rl "26000" web/src | wc -l` returns 1
   (it returns 2 today — `CodeGraphView.tsx:66` and `LinksGraphView.tsx:48`).
6. `/` focuses find; matches stay lit, the rest dim, the viewport eases to fit.
7. Every node is reachable by `Tab`, selectable by `Enter`, and carries an `aria-label`
   naming its kind, path, connection count, and holder.

**Presence**
8. An agent taking a reservation on `services/items.py` makes that node glow in its owner's
   `User.avatar` color within one heartbeat interval.
9. When the reservation lapses, the glow disappears **without any sweeper running** — proven
   by a test that advances the clock and asserts the presence payload empties.
10. A held node still renders its kind color and its described/stale stroke unchanged.
11. Two agents from **different** users on one node produce a contention ring and an
    inspector line naming both; two agents from the **same** user produce neither.
12. `prefers-reduced-motion` yields a static ring, no animation.
13. Clicking a legend chip solos that user's clouds and highlights their held nodes.

**Query**
14. `hubs()` returns the node with the most inbound edges, and the Hubs panel ranks it first.
15. `path(a, b)` returns a typed edge list, and the graph dims everything off the path.
16. `graph_query` is callable over MCP and appears in the tool count that `test_docs_sync.py`
    ratchets.

## 9. Sequencing

| Phase | Contents | Why this order |
| --- | --- | --- |
| **P1** | D1 (extract + worker) + AC-3 (stable positions under filtering) | Everything else renders into this surface. Shipping presence onto a graph that reshuffles on every toggle would make the glow look like a bug. |
| **P2** | D2, D3 — pan/zoom/drag/find, hover, keyboard, a11y | The interaction floor. Independently valuable, and the only part that helps at today's node counts. |
| **P3** | D4, D5 — the held set and the clouds | The feature. Needs P1's stability to read as presence rather than churn. |
| **P4** | D6, D7 — contention ring, fleet legend | Needs P3. D6 in particular is only meaningful once clouds are trusted. |
| **P5** | §6 — hubs, clusters, path | Fully independent; can land any time after P1 and slots in wherever there is room. |

## 10. Risks

- **The cloud layer at scale.** A blurred radial per held node is cheap at 10 and expensive at
  200. Mitigation: cluster held nodes by holder and emit one hull per (user, component) rather
  than per node — which is also the better *visual*, since a fleet working an area should read
  as one region, not a scatter of dots.
- **Color collision with light backgrounds.** `User.avatar` defaults to `#a78bfa` and users
  may pick anything. On our dark surfaces a 15%-opacity cloud is safe, but two users with
  near-identical colors are indistinguishable. Mitigation: the legend is the ground truth for
  *whose*, and the org settings page should warn on a color within a small ΔE of an existing
  member's — a one-time check at pick time, not a render-time remap.
- **Presence honesty.** The glow is only as true as the heartbeat. An agent that dies mid-lease
  keeps its glow until `expires_at`, by design (§1.3). This is correct — the lease *is* still
  held, and that is a real fact about the codebase, not a stale render. It should be said in
  the inspector: show time-remaining, not just the holder, so a human can tell "working" from
  "about to lapse".
- **Scope creep into a graph library.** The pull toward d3-force or cytoscape will be strong
  around P2. The determinism requirement is the reason to resist: our layout is stable across
  renders by construction, and most libraries are not.
