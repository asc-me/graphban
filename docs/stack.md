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
- The code graph is built by `gban code map`, not by this page: it posts one node per tracked non-binary file at HEAD plus import edges, keeps hand-written summaries, and marks files gone since the last run stale.
- The graph is refreshed hourly from origin's default branch (`gban code map --since`) with a nightly full reconcile at 03:00 local, by a user-level launchd/systemd unit. Install it with `gban code map service install [--repo .] [--hourly-at :MM] [--nightly-at HH:MM]`; `gban code map service status` prints the last run, its outcome, the sha mapped and how far behind origin it is; `uninstall` removes the unit and the key file.

## Reading what you see

- Liveness is three-state: reachable, unreachable, or **unknown**. There is no healthy default.
  A box that is configured but not adjudicating reads uncalibrated.
- Map freshness is likewise: mapped (current), stale (mapped at an older commit than head), unmapped, or unknown.
- "Stale" is compared against the project's code-graph revision (the one commit every node was described at), not the Graphban box's build sha. A graph with no nodes or mixed revisions has no revision, so map and scan freshness read unknown; an explicit `head_sha` overrides it. The instance identity rows still show the build sha.
- "No facts reported" and "No edges" mean none were reported, not that none exist.
- Refactor (complexity) scans: `radon` for Python components and `lizard` (recorded as scanner id `complexity`, CCN > 10, `node_modules`/`dist` excluded) for JS/TS components; `gban scan --dry-run` lists which are installed. A missing lizard records `tool_missing`, never clean.
- Security scanners by manifest (`gban scan`): `pip-audit` only for a component with a Python dependency manifest (`pyproject.toml`, `setup.py`, `requirements*.txt` or `uv.lock`); `bandit` for any component with `.py` files; `pnpm audit` when a `pnpm-lock.yaml` exists; `npm audit --json` when there is a `package-lock.json` and no pnpm lockfile (both recorded as scanner id `npm-audit`). A missing tool records `tool_missing`.
- Dead-code scanner `knip` (JS/TS components) needs `pnpm install` in the component's checkout: `gban scan` symlinks that checkout's `node_modules` into its temporary scan worktree (the link is removed with the worktree; the source `node_modules` is never deleted). With no `node_modules` in the checkout the row records `tool_missing`, never clean.
- Retirement (GRPH-1846): a scanner that no longer applies at a component path (a component dropped Python, a scanner left `SCANNERS`) would hold the cell at its last row forever, since no newer row ever replaces it. `gban scan` reads the path's current rows via `scan_status` and posts `record_scan` with `result: "retired"` for each scanner that has a current row there but is not in this run's plan. A retired scanner's rows stay in history (`current: false`) but make up no part of the cell. Retirement never reads clean: a cell whose scanners are all retired is `never_scanned`. A later row from the same scanner un-retires it. Retirement applies only to `gban scan`'s own scanners (`SCANNERS`): the CLI never retires another producer's row, the server refuses a `retired` row for any other scanner id, and a such row already stored is ignored on read. A retired row's own state is `not_applicable`, never `clean_at_sha`. A scanner that is still planned but failed (`tool_missing`, `incomplete`) is never retired.
- "No findings reported" is not a pass if the scan matrix shows never-scanned cells.
- Empty states name what is missing (no fleet, no sync).

## Related

[code-graph.md](code-graph.md), [outposts.md](outposts.md), [fleet.md](fleet.md), [deploy.md](deploy.md).
