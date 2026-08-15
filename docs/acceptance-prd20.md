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
| 1 | Open Code graph, wheel-zoom onto a node | Zooms **to the cursor**, clamped 0.25×–6×; the thing under the pointer stays under it | |
| 2 | Drag the canvas, then double-click empty space | Pans; double-click resets to identity | |
| 3 | Drag a node, then refresh the map | Node stays where you put it, ringed; on the next layout the rest settles **around** it | |
| 4 | Toggle an edge-type chip | Edges disappear; **no node moves**. This is the one users notice most | |
| 5 | With a filter active, press "Re-layout to visible" | Only offered under a filter; recomputes from visible edges; pins survive | |
| 6 | Press `/`, type a symbol name | Matches lit, rest dimmed, viewport eases to fit; count shown. **Zero hits must dim the graph and show `0`**, not look like no search | |
| 7 | Tab into the graph, then arrow-key around | ONE tab stop into the SVG, then arrows move node to node; Enter selects; Esc clears. Shift+arrows pan | |
| 8 | Shift-click a selected node twice | Reach widens a ring each time; inspector reads `3 hops · N nodes`; edges light only when BOTH ends are in reach | |
| 9 | Start an all-in-one agent; let it `claim_cluster` | Its held nodes glow in **your** avatar colour within one heartbeat (~50s) | |
| 10 | Read the node it is holding | Still shows its kind colour and described/stale stroke — the cloud never tints the node itself | |
| 11 | Open the inspector on a held node | Names the agent, the role, and **time-remaining** counted from `served_at` | |
| 12 | Let the lease lapse without killing the agent | Glow disappears with **no sweeper running** — purely the lease clock | |
| 13 | Have an agent claim an item whose touchpoints include `docs/` or `AGENTS.md` | Legend reads "N held areas not on this map"; tray lists the **raw area text** and the holder | |
| 14 | Second human's agent claims a colliding area | Contention ring in red; inspector leads with "held by 2 agents across 2 users" | |
| 15 | Run two of YOUR OWN agents on one node | **No ring.** Same colour, one denser cloud. If this rings, the alarm is worthless | |
| 16 | Click a legend chip | Solos that human — their clouds stay, others fade to 4%; their held nodes light | |
| 17 | Turn on `prefers-reduced-motion` | Ring stays at mean opacity, stops pulsing. Presence must not vanish for someone who asked for less motion | |
| 18 | Open the Hubs panel | Ranks by **inbound** degree. A file importing forty things must not outrank one forty things import | |
| 19 | `⌥`-click a second node | Shortest path; hops report which way each edge actually points; off-path dimmed | |
| 20 | Ask `graph_query` from an agent terminal | `hubs`/`components`/`path` answer; an unknown `edge_types` is **refused**, not silently narrowed | |
| 21 | Synthesise >800 nodes in several components | Collapses to super-nodes labelled by anchor; clicking one enters it; breadcrumb offers the way back | |
| 22 | With the galaxy collapsed, search for a node inside one | Find sees it and **enters** the component holding it | |

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
