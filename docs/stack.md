# Stack

**Stack** is the derived architecture of what runs, where, and what depends on what:
deployables and external services, not modules (that is the Code graph).

## What the page is for

See the boxes that make up this deployment, whether each is reachable, and its code health.

- Boxes are grouped by host: Compose/Railway (web, api, db), agent hosts (fleet plus vendor
  CLIs), user machines (the CLI), and wired external services.
- A box is a deployable with its own lifecycle. MCP is a surface on api, not its own box, and
  pgvector is an extension inside Postgres.

## Main actions

- **Click a box** to open the inspector: facts with provenance, surfaces/ports, extensions, and
  protocol-labelled connections.
- **Collapse** the Graphban group when you only care about externals.
- **Scan report / code health**: per-box findings, filterable. A finding can be filed as cleanup
  work for an agent with the repo; the server never runs scanners itself. Infrastructure and
  external services are not scanned and show "Code health · N/A", liveness only.
- **Print / PDF** exports the current theme at 1:1.
- A banner shows whether the app topology is mapped. If not, run `gban stack map`.

## Reading what you see

- Liveness is three-state: reachable, unreachable, or **unknown**. There is no healthy default.
  A box that is configured but not adjudicating reads uncalibrated.
- Map freshness is likewise: mapped (current), stale (mapped at an older commit than head), unmapped, or unknown.
- "No facts reported" and "No edges" mean none were reported, not that none exist.
- "No findings reported" is not a pass if the scan matrix shows never-scanned cells.
- Empty states name what is missing (no fleet, no sync).

## Related

[code-graph.md](code-graph.md), [outposts.md](outposts.md), [fleet.md](fleet.md), [deploy.md](deploy.md).
