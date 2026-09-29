# Settings & profile

**Settings** (`/settings`, or the top-bar user menu) is a tabbed view; **Profile**
(`/profile`, from the user menu) shows the current user and their access.

## This box (self-host)

Self-host Settings groups instance-wide items under **This box** (do not rename that
group). AI providers is which model the box runs on (GRPH-625); Cloud / Sync is
instance-wide; unlinked Gitops is this project's process; Updates is whether this box is
on the published stable cut; Log export is where the box's telemetry goes (GRPH-966).

### Updates

`/settings/deployment/updates`. **Check for updates** refetches
`GET /api/platform/update-check`. **Install** is on the self-host page and is
enabled when `apply` is true and a newer cut is advertised. Compose apply is a
**host helper** (`scripts/graphban_compose_host.py`) that runs
`scripts/deploy.sh --local` — the API container does not get a Docker socket.
Native apply fetches the GitHub Release tarball and starts
`graphban_host.py upgrade` when `/opt/graphban/current` exists. No helper and
no native tree means `apply` is false, not a green Install. Hosted has Check
and no Install.

Three states: **current** (the page says this box is on the latest release),
**available**, **unknown**. Unknown is not current: a failed feed fetch, or a
running version that is still `0.1.0`, must not look up to date.

The page names **how this box is deployed** (compose helper / native / hosted /
could not tell — empty `via` is not compose). **Release notes** are the GitHub
Release body for this cut, in an accordion. When an update is available, a
second list is the latest tag — not a rollup of skipped cuts. An empty body is
“no notes on this release”; a failed fetch is “could not load notes”.

Cutting a named CalVer so this page has something to Install is
[release.md](release.md) (`stamp` then `publish`). GitHub's source zip is not
a release. Apply stays this button — not the publish script, not MCP.

### Gitops

`/settings/deployment/gitops`. Sparse delivery contract: integration base, whether
agents may push to it, optional branch/PR naming, reviewer bar, version scheme,
and where the cut process is written (`release_defined_in`).

- A **model** picker (Unmeasured / Push to base / PRs to base / PRs to integration)
  writes the preset fields in one save. `base_branch` and `release_defined_in` are
  never filled by a preset — a model save with an empty base is 422, not a write of
  `main`, and unmeasured locator is not `docs/release.md`. A later hand-edit
  clears the model id.
- Saving a non-Unmeasured model on a **project** files a tracker parent + children
  (observe / remote / base_branch / optionally stop-pushing, one PR, tagged cut /
  contract is live). Re-applying the same model does not duplicate. Org-house apply
  does not file per-project plans. GET gitops `plan` names the parent item; get_context
  does not. Graphban still does not run git. Observe answers
  `remote` / `none` / `unknown` as evidence; the rest stay blocked until remote or
  none. `github_repo` is not a remote. Children 2–5 accept evidence `already` when
  the repo already matches — that is a receipt, not an auto-done.
- Unset fields are **unmeasured**, never "use main" and never "no requirements".
  Placeholders say `Unmeasured — not main`.
- The page renders `GitopsView` from `GET /api/projects/{id}/gitops`.
  `control.writable` disables inputs; `control.message` is the only banner. The page
  does not decide linked-ness itself.
- When linked, fields stay visible and filled but grey. Local columns appear as muted
  `was:`, not as the form value.
- `no_push_to_base` is a tri-state (Unmeasured / Yes / No), not a checkbox —
  unchecked would mean "you may push".
- Naming tokens `{item_id}` `{tag}` `{slug}` `{version}` `{date}` insert on branch/PR
  patterns only. `base_branch` is a literal (chips `stage` `test` `main` `develop`).
  Globs (`*` `?` `[`) are 422.
- Unlinking restores this box's pre-link values as live, with a warning they are not
  the org's last contract. They are not wiped.

Hosted orgs edit house process on the org-admin Gitops tab, not this page.

### Log export

`/settings/deployment/log-export`. Where this box hands telemetry to an OTLP collector
(PRD-47 S15 / GRPH-966). **One config for the deployment, not one per project** — one
collector, one exporter, one queue — so `log_export_config` has no `project_id` column and
no route takes one. Records span projects, so each RECORD names its project (`gb.project`,
`gb.project_id`, and `gb.scope` for the deployment-level ones).

Gated the way the other This-box panels are: a logged-in operator (JWT, never an API key).
Every route 403s on a hosted instance, where the operator configures the box; the web UI
redirects the path away rather than showing a form that can only fail.

**The status strip has a third answer for every number**, which is the point of the page:

- `sent 24h` / `dropped 24h` are sums over `log_export_batches`. `0` means measured and
  nothing was sent; `—` means the read failed. A counter column could not tell those apart.
- `queue depth` is `—` whenever no exporter is running. A backlog nobody is draining is not
  an empty queue, and the page says so rather than printing a zero.
- `state` is **Exporting** / **Paused** (off) / **On, but nothing is draining** / **Unknown**
  (the config could not be read). Off and unknown are different states.
- A failed fetch is `PlannerError`, never a panel that reads as off.

**Collector** — endpoint (the base, as `OTEL_EXPORTER_OTLP_ENDPOINT` defines it; records go
to `/v1/logs`, `/v1/traces`, `/v1/metrics` under it, so it needs a port), protocol,
compression, and headers. Header values are stored as typed and shown masked; saving a
masked value back keeps the stored one, and removing a row deletes the header.

**Send test batch** always answers — a collector that cannot be reached is the answer, not
an API error. `ok` is true only when records were accepted, and then reports the count and
latency. Failures carry a specific code: `no_port` ("No port in endpoint — collector
unreachable"), `no_endpoint`, `unreachable`, `bad_status`, `timeout`, `unsupported`. Not-run
is its own state and does not look like a pass. A probe is written as a `kind="test"` batch
row, so it shows as *last batch* but never counts toward `sent 24h`.

This build sends **OTLP/HTTP with the JSON encoding**. The protobuf encoding needs
`opentelemetry-proto` and gRPC needs `grpcio`; neither is a dependency here
(`providers/llm_meter.py` records the same call about OTel), so gRPC is published as
unsupported *with the reason* rather than being silently downgraded.

**What to send** — activity events as logs, MCP tool calls as traces, agent heartbeats as
metrics, plus an event-type filter. An empty filter means every action, and the page says so;
the options are measured from the ledger, so it never offers an action this box does not
record. **Redaction** — drop free-text summaries, drop client IPs, mask API keys. The sample
record below them is recomputed by the server on every toggle, so it shows what the exporter
would send rather than what a second implementation guessed. Secrets, home paths and
addresses inside free text are redacted whatever the toggles say: a collector is off-box.

Enabling starts from the current high-water mark rather than back-filling the ledger, and
says so. A failed batch keeps its records; after five consecutive failures the backlog is
dropped **and counted**, and that give-up resets the streak so the exporter asks the
collector again. The drain interval is `LOG_EXPORT_SECONDS`
([configuration](configuration.md)); `0` disables it, which the page reports as *not
running*.

## AI Providers tab

**Where it lives:** hosted → the **AI Providers** tab. Self-host → **This box → AI
providers** (`/settings/deployment/providers`), beside Cloud / Sync, Gitops, Updates and Log
export — provider credentials are what the box runs on, not per-project config (GRPH-625).
The old `/settings/project/providers` deep link redirects to the new path.

Switches the **chat & extraction** provider — takes effect immediately.

- Pick a mode: **Offline stub** (default, deterministic, no external services), **Local
  (Ollama)**, or **Cloud (Claude)**.
- Local mode exposes the Ollama base URL + chat model; cloud mode exposes the Claude model.
- **Save** persists to `platform_config` and reapplies to the live provider (the in-memory
  settings are updated and the provider cache is reset).

> The **embedding** provider is deliberately a **deploy-time** setting, not switchable here —
> changing it changes the pgvector column dimension. See [AI providers](ai-providers.md).

## Integrations tab

### GitHub

- **Connect** stores account + repo (and shows scope) **for this project**. **Disconnect** clears it.
- The tab shows the **inbound issues webhook** URL (`…/api/public/github/webhook`) with a
  copy button. Point a GitHub *Issues* webhook at it and **opened issues become tracker
  items** (rate-limited; real deployments add HMAC signature verification).
- **Repo → project routing:** the webhook reads the payload's `repository.full_name` and
  creates the item **in the project that has that repo connected** (falling back to the
  default project). Each created item is **linked back** to the originating issue via
  `github_url`, shown as a GitHub chip on the tracker row.
- **Note:** connection state and the inbound webhook are fully wired. **Outbound** sync
  (opening real GitHub issues, two-way PR sync) requires a connected token/OAuth and is out
  of scope for the local slice — `POST /api/platform/github/create-issue` creates the local
  item and reports `pushed_to_github: false`.

### Google Drive

A connect form (account + folder) plus a **Sync now** button. Sync is **real** — the engine
mirrors PRDs to markdown files and imports them back, with conflict detection. It's
**filesystem-backed**: the container mounts a host directory (`SYNC_DIR`, default `/data/sync`,
mapped from `SYNC_HOST_DIR` — default `./sync`). **Point that host directory at a Google Drive
Desktop folder and PRDs reach Drive with no OAuth.** The sync engine talks to a `SyncBackend`
interface, so a native Drive-API backend can be added without touching the reconcile logic.

**Folder structure.** The connection is per-project; the folder name you pick is a subfolder of
`SYNC_DIR`, and that folder is the project's root:

```
<SYNC_DIR>/<folder>/
└── PRDs/    Each PRD as "<PRD-id> — Title.md" with a front-matter block
              (graphban_id / title / status / version)
```

(`Digests/`, `Exports/`, `Attachments/` are reserved for future sync of those artifacts.)

**Two-way sync (Sync now).** The reconcile is conflict-safe via a per-PRD last-synced hash:

- A PRD with no file yet → **exported** to `PRDs/`.
- A `.md` file with **no** `graphban_id` → **imported** as a new draft PRD (title from the first
  `# heading` or the file name), and the id is written back into that same file so it isn't
  re-imported. This is the same import the [PRD page](prds.md) exposes manually.
- Only the **file** changed since last sync → the PRD is updated and a version is snapshotted.
- Only the **PRD** changed → the file is rewritten.
- **Both** changed since last sync → a **conflict is flagged** (reported in the sync summary) and
  *neither side is clobbered* — resolve it and sync again.

The **Sync now** button reports counts (exported / imported / updated / in-sync) and any
conflicts. Deleting a file doesn't delete the PRD.

## Project tab

Edit the active project's **name**, **description**, and flags:

- **Share global memory across projects**
- **Auto-extract lessons on item completion** (drives [auto-extraction](memory-and-chat.md#auto-extraction-on-done))
- **Expose MCP tools for this project**

Saved via `PATCH /api/projects/{id}`.

## Members tab

Lists the project's members with their **role** (owner/admin/member) and **access**
(write/read/none), from the `memberships` table.

## API Keys tab

Manage scoped keys used to authenticate agents to the [MCP endpoint](mcp.md):

- **Create** a key — the plaintext (`gb_sk_…`) is shown **once**; copy it immediately. Only a
  SHA-256 hash is stored.
- **Revoke** a key with the trash icon.

Keys minted before the Graphban rename start `al_sk_` and keep authenticating — the accepted
prefixes only ever grow, so nothing needs re-issuing ([configuration](configuration.md)).

## Profile (`/profile`)

Your account card (name, handle, email, avatar) and **project access** — each project you
belong to with your role and access level. Reachable from the top-bar user menu.

## Hosted Admin → Gitops

On a hosted org, **Admin → Gitops** is the write surface for the delivery contract after a
box is linked. The page is org-scoped (`adminPath("gitops")`); it is **not** a field on
Deployments cards — the sync credential is identity (PRD-21 D6), gitops is house process.

- **House process** (top) is `GET/PATCH /api/orgs/{id}/gitops`. Unset is **unmeasured**, never
  `main`. Sparse fields are inheritance; `{}` does not wipe.
- **Per-project overlay** iterates `GitopsView.projects` from that org GET — every org
  project, including ones the admin does not sit on. It does **not** use the readable-only
  project list (`useProjects` / galaxy overview). Each row `GET`s `/api/projects/{id}/gitops`.
- Overlay inputs bind to `source === "project"`. An inherited org `stage` is an empty input
  plus muted “inherits stage”, not `stage` in the field. Saving with no edits sends `{}` and
  does not copy the house value onto the project. × sends JSON `null` (clear → inherit).
- One failed overlay GET leaves that row unmeasured with “could not load overlay”; the house
  form still loads.
- Members do not see the Admin group, including Gitops. PATCH is `require_org_admin` (403).

## How it works

- Config: the `platform_config` table (Alembic migration `0004`), one row per project —
  except log export, which is one row for the box (`log_export_config`, migration `0134`).
- Service: `backend/app/services/platform.py` — `get_config`, `update_config` (applies LLM
  settings to the live provider), and the GitHub/Drive connect/disconnect helpers. Log export
  is `backend/app/services/logexport.py`, which also owns the exporter drain.
- Routers: `backend/app/routers/platform.py`, plus project/member routes in
  `projects.py`, deployment settings in `settings.py`, and `GET /api/auth/me/memberships`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET / PATCH | `/api/platform` | Read / update platform + provider config |
| POST | `/api/platform/github/connect` · `/disconnect` | GitHub connection state |
| POST | `/api/platform/github/create-issue` | Mirror an item as an issue (local; honest stub) |
| POST | `/api/platform/gdrive/connect` · `/disconnect` | Drive connection state |
| PATCH | `/api/projects/{id}` | Update project config |
| GET / PATCH | `/api/projects/{id}/gitops` | This project's gitops contract (linked reads are org-live) |
| GET / PATCH | `/api/settings/log-export` | The deployment's OTLP export config, status strip and sample (no project id — see [Log export](#log-export)) |
| POST | `/api/settings/log-export/test-batch` | Send test batch; always 200 with a result, `error` names the failure |
| GET | `/api/settings/log-export/sample` | The sample record under redaction flags the caller has not saved |
| GET | `/api/projects/{id}/members` | List members |
| GET / POST / DELETE | `/api/api-keys` … | Manage API keys |
| GET | `/api/auth/me/memberships` | The current user's project access (Profile) |
