# PRD-20 acceptance walk — the living graph

D1–D9 are shipped and tested. **Everything they assert was verified by tests I wrote against
code I wrote**, on branch `p20/d1-layout-owner`. This walk is the first time a human drives it,
and this repo's history says that is where the real defects are.

Two things sharpen where to look this time.

**The sabotage pass found five defects the green suite did not**, and four of them were in my
own tests rather than in the code: two vacuous assertions on D2, one on D8, redundant dead code
on D4. A test that passes under both the correct and the broken transformation is exactly what a
walk is for — so run this looking for the things a test structurally cannot see.

**The presence half renders nothing until real agents hold real leases.** Every presence test
below constructs `AreaReservation` rows directly. Steps 9–17 are the first time the join runs
against reservations a live `claim_cluster` actually wrote, and the coverage numbers say what to
expect: the live code graph is **123 nodes against 401 source files (30%)**, and **15 of 100
touchpoints on real backlog items resolve to no node at all**. The off-map tray is not an edge
case here. It is the common case.

---

## Setup

Run against **`ubuntu-srv`** — `http://192.168.50.81:8080` — which has the real 431-item
backlog and a populated code graph. A scratch stack cannot produce the coverage gaps that
steps 13 and 17 exist to surface.

The all-in-one posture now claims through the divvy (GRPH-380), so a single pasted agent
prompt produces reservations. Before that change this walk was impossible to run at all on a
default install: `claim_cluster` is the only writer of `AreaReservation`, and the prompt said
`claim_next`.

For step 4 you need a graph larger than the live one. Synthesise it rather than describing 800
real files — the criterion is about layout latency, not about coverage.

---

## Mechanically verified before the walk

These ran on the branch and are recorded so the walk does not re-derive them.

| AC | Check | Result |
| --- | --- | --- |
| 4 | First layout at 800 nodes ≤1.5s | ✅ **446ms** (42 iterations). Also 123 nodes → 91ms, 400 → 454ms |
| 5 | `computeLayout` in exactly one file | ⚠️ **PRD command wrong** — see below |
| 12 | One file area resolves to ONE node | ✅ `area_matches` → 1; `clustering._match` → 7 on the same 7-file sample (25 on the live `services/` directory) |
| 18 | Presence refuses an agent API key | ✅ 401; and no `fleet_presence` tool exists on the MCP surface |
| 20 | `components()` is deterministic | ✅ identical across repeated reads; asserted with the same fixture in both suites |
| 22 | `graph_query` over MCP, counted by the docs ratchet | ✅ 53 tools; `docs/mcp.md` updated |

**AC-5 as written in the PRD is wrong.** `grep -rl "26000" web/src | wc -l` returns **2**, not 1
— the second hit is the parity fixture in `graph-layout.test.ts`, which deliberately keeps the
old constant to prove the extraction moved no node. The checkable forms are
`grep -rl "26000" web/src --exclude-dir=__tests__ | wc -l` → **1**, or scoped to
`web/src/features` → **0**. Amend the AC; do not delete the fixture.

---

## The walk

| # | Step | Expected | Result |
| --- | --- | --- | --- |
| 1 | Open Code graph, wheel-zoom onto a node | Zooms **to the cursor**, clamped 0.25×–6×; the thing under the pointer stays under it | ✅ clamps exactly **0.25×–6×**; cursor drift −0.24/−0.49 world units at k=1.82 — float residue |
| 2 | Drag the canvas, then double-click empty space | Pans; double-click resets to identity | ✅ pan changed the transform; double-click restored exact `translate(0,0) scale(1)` |
| 3 | Drag a node, then refresh the map | Node stays where you put it, ringed; on the next layout the rest settles **around** it | ✅ pinned to the float, gained an r=10 ring, class → `cursor-grab`, aria gained `, pinned` |
| 4 | Toggle an edge-type chip | Edges disappear; **no node moves**. This is the one users notice most | ✅ **303 → 154 edges, 0 of 228 nodes moved** |
| 5 | With a filter active, press "Re-layout to visible" | Only offered under a filter; recomputes from visible edges; pins survive | ✅ appeared only once a chip was off; pin held exactly, **227 others re-settled** around it |
| 6 | Press `/`, type a symbol name | Matches lit, rest dimmed, viewport eases to fit; count shown. **Zero hits must dim the graph and show `0`**, not look like no search | ✅ count `1`, viewport eased; **zero hits → badge `0`, all 228 nodes dimmed to 0.22** |
| 7 | Tab into the graph, then arrow-key around | ONE tab stop into the SVG, then arrows move node to node; Enter selects; Esc clears. Shift+arrows pan | ✅ **after GRPH-479** (was two stops, and arrows never moved DOM focus). svg `tabindex=0`, **0 focusable inside**; 3× ArrowRight → `activeElement` = `File backend/app/models/__init__.py, 22 connections` |
| 8 | Shift-click a selected node twice | Reach widens a ring each time; inspector reads `3 hops · N nodes`; edges light only when BOTH ends are in reach | ✅ 2 hops · 45 nodes → **3 hops · 81**; 88 lit / 66 dim, **zero violations either way** |
| 9 | Start an all-in-one agent; let it `claim_cluster` | Its held nodes glow in **your** avatar colour within one heartbeat (~50s) | |
| 10 | Read the node it is holding | Still shows its kind colour and described/stale stroke — the cloud never tints the node itself | |
| 11 | Open the inspector on a held node | Names the agent, the role, and **time-remaining** counted from `served_at` | |
| 12 | Let the lease lapse without killing the agent | Glow disappears with **no sweeper running** — purely the lease clock | |
| 13 | Have an agent claim an item whose touchpoints include `docs/` or `AGENTS.md` | Legend reads "N held areas not on this map"; tray lists the **raw area text** and the holder | |
| 14 | Second human's agent claims a colliding area | Contention ring in red; inspector leads with "held by 2 agents across 2 users" | |
| 15 | Run two of YOUR OWN agents on one node | **No ring.** Same colour, one denser cloud. If this rings, the alarm is worthless | |
| 16 | Click a legend chip | Solos that human — their clouds stay, others fade to 4%; their held nodes light | |
| 17 | Turn on `prefers-reduced-motion` | Ring stays at mean opacity, stops pulsing. Presence must not vanish for someone who asked for less motion | |
| 18 | Open the Hubs panel | Ranks by **inbound** degree. A file importing forty things must not outrank one forty things import | ✅ **after GRPH-480.** `config.py` 9←/0→ ranks 3rd; `mcp_server.py` 5←/18→ ranks 6th, with the inversion callout. Undirected degree would swap them |
| 19 | `⌥`-click a second node | Shortest path; hops report which way each edge actually points; off-path dimmed | ✅ **after GRPH-481.** `mcp_server.py → AGENTS.md → queries.ts`: `↑ references points back`, then `↓ points this way`. **2 of 303 edges lit** |
| 20 | Ask `graph_query` from an agent terminal | `hubs`/`components`/`path` answer; an unknown `edge_types` is **refused**, not silently narrowed | ✅ `hubs`/`components`/`path` all answer; `edge_types: [summons]` **refused by name**, listing the valid five |
| 21 | Synthesise >800 nodes in several components | Collapses to super-nodes labelled by anchor; clicking one enters it; breadcrumb offers the way back | |
| 22 | With the galaxy collapsed, search for a node inside one | Find sees it and **enters** the component holding it | |

## Result — 11 of 22 measured, all passing; 4 defects found and fixed

Driven 2026-08-22 and 2026-08-24 against **`ubuntu-srv`**, the live instance — first on
`9d1936b`, then again on `ace2cd0` once the fixes it produced had shipped.

**Eleven steps have results. Three of them FAILED when first run, and all three now pass on the
deployed build** — steps 7, 18 and 19. The other eleven are not passing; they have not been run,
and the section below says why for each, because eleven empty cells and eleven passing ones look
identical in a summary.

## What has not run, and why

**Steps 9–17, the presence half.** These need a real `claim_cluster` writing real
`AreaReservation` rows, a lease clock left to lapse in real time, and — for step 14 — a SECOND
human's agent, which means a second user account on the instance. The first two are only tedious.
The third is not something an agent can provision for itself.

**Steps 21–22, the galaxy.** These need a graph past the 800-node detail budget. The live graph is
193. Synthesising 800 nodes means writing 800 descriptions nobody wrote into the live code graph —
corrupting the thing being measured in order to measure it. This wants a scratch instance.

## Defects this walk found

| # | What | Filed | State |
| --- | --- | --- | --- |
| 7 | The graph cost **two** tab stops, not one: `<svg tabIndex={0}>` plus a roving node also at `0` | GRPH-479 | fixed, #299 |
| 7 | Arrows moved the focus **ring** and the tabindex, never `document.activeElement` — so every `aria-label` in the view went unannounced | GRPH-479 | fixed, #299 |
| 18 | No Hubs panel existed. The frontend's only ranking was `degrees()`, documented as **undirected** — the measure AC-18 forbids | GRPH-480 | built, #300 |
| 19 | No path interaction existed. `altKey` appeared once in `web/src`, as a guard that *ignores* alt | GRPH-481 | built, #301 |

**Why a green suite missed all four.** The keyboard tests asserted on `focusId` — the state the
code sets — rather than `document.activeElement`, the thing assistive technology reads. Assertion
and implementation shared one wrong idea, so they agreed. And "exactly one tab stop" was checked
by a `renderHook` test, which has no container: it verified *"exactly one NODE is tabbable"*, which
was true and was never the requirement.

Steps 18 and 19 had no test to be wrong. The capability was complete and correct on the server —
`graph_query` answers both — and only a walk that opens the UI can tell the difference between
*the product can do this* and *a person can reach it*.

## Method notes worth carrying

**Three of my own measurements were wrong before any of the code was.** Step 1 first read as a
42-unit cursor drift, because `clientToView` scales into viewBox units and I compared pixel space.
Step 8 first read as "all 154 edges lit", because edges dim via `stroke-opacity` and I measured
`opacity`. Step 5 first read as "227 nodes moved" when the pinned node had simply changed class
and shifted every index. Each would have been a false defect report; each was caught by checking
the instrument against the source before believing it.

---

## What I expect to fail

Recorded before running, so the walk grades the prediction too.

**Step 9 is the one I would bet against.** Every presence test writes `AreaReservation` rows
directly. The first real reservation comes from `claim_cluster`, whose areas come from
`collision.touch_areas()` — which returns `predict_areas()` GUESSES when an item has no
touchpoints. So the first thing this shows may be a dashed predicted cloud in a place nobody is
editing. That is the design working, but it will not look like it.

**Step 13 will show a bigger number than feels right.** 15% of live touchpoints resolve to
nothing, and `vercel env` and `twitch developer console` will sit in that tray looking like
bugs. They are not. They are areas that are not repo paths, and the tray exists to say so.

**Step 15 is the cheapest way to invalidate the whole feature.** If two of one person's own
agents ring each other, the alarm fires constantly on the normal single-developer case and
everybody learns to ignore red. The unit test covers it; the walk covers whether `user_id`
actually resolves the same way through two live registrations.

**Step 18 has a data problem the code cannot fix.** The live graph is 119 `module`, 4 `file`,
0 `symbol` — so the kind channel D5 argues so carefully to protect is ~97% one colour today.
Hubs will rank correctly and the graph will still look monochrome. GRPH-382 is filed for it.
