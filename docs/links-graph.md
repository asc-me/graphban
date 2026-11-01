# Links graph

The **Links** view is an interactive graph of **typed relationships** between tracker items
and requests — dependencies, shared code, semantic similarity, and shared tags.

## Link types

| Type | Color | Meaning |
| --- | --- | --- |
| `dependency` | `#c6f24e` | One item depends on another |
| `code` | `#7ca2ff` | They touch shared code |
| `semantic` | `#a78bfa` | Conceptually related (e.g. reuse vectors) |
| `tag` | `#e0b34a` | Share a tag / theme |

Each link has a `confidence` (0–1) and a human `reason`.

## Using it

- **Nodes** are items (`AL-…`, lime) and requests (`R-…`, teal). **Edges** are colored by
  type.
- **Filter** — toggle the type chips top-right to show/hide edge types.
- **Inspect a node** — click it to highlight its neighborhood (its edges + connected nodes,
  dimming the rest) and open a detail card listing each connection with its reason.
- **Inspect an edge** — click it to see the two endpoints, type, confidence, and reason.
- **Layout** — nodes are placed by a small **deterministic force-directed** simulation
  (repulsion + edge springs + center pull), so the graph is stable across renders (no
  randomness).

## How it works

- Data comes from `GET /api/links` (the `links` table; typed links are also created by the
  `link_items` MCP tool — see [MCP tools](mcp.md)).
- Service: `backend/app/services/links.py` (`create_link`, `list_links`).
- Frontend: `web/src/features/links/LinksGraphView.tsx` draws the SVG. Placement comes
  from `useGraphLayout` in `web/src/lib/graph/`, the same deterministic force-directed
  layout the code graph uses (no graph library).

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/links` | List typed links (optional `?project_id=`) |

Create links via the `link_items` MCP tool, or `create_link` in the service layer. (A
dedicated REST create endpoint and embedding-based auto-suggestion are natural follow-ons.)
