# Stack view — design contract (GRPH-1034 rebuild)

Source of truth: `docs/design/Stack.dc.html` (the approved Claude Design export,
project `agentledger - local`, design system `83ade3bd`). Read it in full and
match it. This markdown is the distilled checklist; the `.dc.html` is the pixels.

## Why this doc exists (read first)

The first build of `web/src/features/stack/StackView.tsx` rendered the topology
as a **static CSS grid of host-group cards** with node cards, and edges only as a
text list inside the inspector. That is a **MISS**. It is model-faithful but it is
NOT the designed view. The approved design is a **spatial, zoomable ring-and-host
map with drawn, protocol-labelled edges**. Rebuild the view to the design below.

The backend (`backend/app/services/stack.py` → `GET /api/platform/stack`) already
emits everything needed: nodes with `ring`, `host`, `scope`, `surfaces`,
`extensions`, `liveness`, `facts` (each with `provenance`), and `edges` with
`from`/`to`/`protocol`/`label`. Do not change the data contract unless a field is
genuinely missing; if so, extend the service + its tests. **Derive layout from the
data — nothing hand-drawn, nothing hardcoded.** The `.dc.html` hardcodes sample
coordinates because it is a mock; the real view must compute the layout from the
returned nodes/edges.

## The canvas (the defining element)

- A single **pan + zoom canvas**, not a document grid. Mouse-wheel zooms toward the
  cursor; `Ctrl/⌘`+wheel zooms faster. Drag to pan.
- Controls (bottom bar): **zoom −**, a **zoom %** readout, **zoom +**, **Fit**,
  **1:1**, and a **level-of-detail** label ("Detail drops as you zoom out so large
  stacks stay readable"). At low zoom, hide tiles/surfaces and fine text; keep the
  box, its name, and its liveness dot.
- Default zoom = **Fit** (whole map visible).

## Three ownership rings (nested frames, not text labels)

Draw three **nested rounded-rectangle frames**, outer→inner, each with a corner
label:
1. `3 · EXTERNAL SERVICES — we only call them` (outermost)
2. `2 · INFRASTRUCTURE WE RUN — we operate it, didn't write it`
3. `1 · CODE WE WRITE — our deployables` (innermost)

A node's `ring` (1/2/3) places it inside the matching frame. The ring is the
**containing frame**, not a chip on a card.

**Ownership is relative to the project being viewed.** Graphban's deployables
(`api`, `web`, `gbfleet`, `gbagent`, `gban`) are ring 1 only for a project whose
code map has described that box's source (`GRAPHBAN_SOURCES` in `stack.py`: specific
files and packages, never a generic `backend/app` or `web/src`). For every other
project they are ring 2, tools the project runs to build, and an `own_code` empty
says the project's own deployables are not on the map. Vendor CLIs are never ring 1.
The code-health footer draws only on ring-1 boxes, because health is keyed by repo
path and would otherwise credit the viewing project's `web/` to Graphban's `web`.

## Host groups (dashed frames inside the rings)

Group boxes by host with a **dashed-border frame** and a mono label, e.g.
`SELF-HOSTED · LAN`. Hosts in the design: `stack` (compose/Railway: web+api+db),
`agent` (gbfleet, gbagent and spawned vendor agents), `user` (gban), `ext` (external
services). The three CLIs are three boxes: `gbfleet` and `gbagent` ship in
`graphban-fleet`, `gban` in `graphban-cli`.
Selecting the `stack` host highlights every node on it.

## Boxes (deployables)

Each node is a **box** sized to its content (the `.dc.html` sample sizes are a
guide, not literal). A box shows: name, kind/subtitle, version line, a **liveness
dot + state**, and the `scope` tag (**instance** vs **project**) where it applies.

- **`api`** renders its layers as **surface tiles inside the box** — REST API
  (`/api/v1 · HTTP+JWT`), MCP server (`/mcp · JSON-RPC`), Sync engine, Provider
  resolution. Surfaces are tiles on the one box, never separate boxes.
- **`db`/database** renders **pgvector as an "inner" chip inside the Postgres box**
  ("Embeddings live in this database. Same process, same backups."), not a peer box.
- `fleet`, `vendor agents` show their outpost/child rows.
- External providers (ollama, decider, xAI, OpenAI, Anthropic, Resend, Drive,
  Linear) are compact boxes in ring 3 — **wired only**; unwired catalog providers
  are not drawn (show the count elsewhere, per the empties rule).

## Edges (drawn, directional, protocol-labelled) — REQUIRED

Draw an **SVG edge per `edges[]` entry**, routed as orthogonal polylines between
boxes, with an **arrowhead** at the `to` end and a **mono protocol label** near the
midpoint. Color each edge by the liveness of its endpoints:
reachable = neutral/green, degraded = amber (dashed), unknown = grey (dashed),
unreachable = red (dashed). The design's edge set and labels:

| from → to | protocol label |
|---|---|
| cli → api | `HTTP · JWT` |
| web → api | `HTTP · JWT` |
| fleet → api | `HTTP + MCP` / `JSON-RPC` |
| fleet → vendor | `spawns` / `local procs` |
| fleet → gbagent | `spawn` |
| gbagent → api | `MCP` |
| api → pg (db) | `psycopg · :5432` |
| api → ollama | `HTTP /api/chat` / `/api/embed` |
| api → decider | `HTTP` / `/v1/systemone` |
| api → xAI | `HTTPS · chat` |
| api → openai | `HTTPS · embed` |
| api → anthropic | `HTTPS · chat` |
| api → resend | `HTTPS · REST` |
| api → drive | `HTTPS · Drive API` |
| api → linear | `HTTPS · GraphQL` |

(These come from the live `edges[]`; render whatever the API returns, labelled.)

## Inspector (click a box)

Floating panel (design: top-right, ~368px). Shows: title + liveness + scope + ring;
**role/placement** prose; **model resolution** for the active project where it
applies; **facts, each with its provenance line**; and **connections as clickable
rows** (direction arrow + peer + protocol) that select the peer. Close button.
This is close to the current build — keep it, dock it to the canvas.

## State & honesty rules (unchanged from PRD §6)

Liveness: reachable / unreachable / **unknown** / (decider) **uncalibrated** /
**not-configured** — never a healthy default. Instance-wide vs project-scoped
labelled on every node it applies to. Empty states first-class ("no fleet
registered", "no cloud provider wired", "N catalog providers unwired").

## Export

- **v1: PDF / print** — the whole map at 1:1 in the current theme (dark or light).
  Print hides the inspector + chrome, prints the canvas.
- **Deferred (do NOT build now):** `.drawio`/Lucidchart export (PRD §4).

## Explicitly deferred (NOT in this item)

- **Code-health overlay** (the "Code health overlay" toggle, per-box health footer,
  scanner categories) → **GRPH-1035**. The `.dc.html` includes it; leave it out of
  this rebuild. The inspector's code-health section is also 1035.

## Themes

Support **dark and light** via the app's existing tokens (the `.dc.html` defines
both `:root[data-theme=dark]` and `[data-theme=light]`). Use app CSS variables, not
the raw hex from the mock.

## Acceptance for the rebuild

1. The view is a pan/zoom canvas with Fit / 1:1 / zoom controls + LOD, not a static
   grid.
2. Three nested ring frames are visible and labelled; nodes sit in the correct ring.
3. Host groups are drawn as frames; boxes are grouped by host.
4. Every `edges[]` entry is a drawn, directional, protocol-labelled edge colored by
   liveness — not a text list.
5. `api` shows surface tiles inside one box; pgvector renders inside the db box.
6. Liveness (incl. unknown/uncalibrated/not-configured) + instance/project scope are
   shown; empties are first-class.
7. Inspector shows facts-with-provenance + clickable protocol connections.
8. PDF/print renders the whole map at 1:1 in the current theme.
9. Everything derived from `GET /api/platform/stack`; no hardcoded topology.
10. Frontend tests + typecheck pass; backend tests stay green.
