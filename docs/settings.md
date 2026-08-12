# Settings & profile

**Settings** (`/settings`, or the top-bar user menu) is a tabbed view; **Profile**
(`/profile`, from the user menu) shows the current user and their access.

## AI Providers tab

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

## How it works

- Config: the `platform_config` table (Alembic migration `0004`), one row per project.
- Service: `backend/app/services/platform.py` — `get_config`, `update_config` (applies LLM
  settings to the live provider), and the GitHub/Drive connect/disconnect helpers.
- Routers: `backend/app/routers/platform.py`, plus project/member routes in
  `projects.py` and `GET /api/auth/me/memberships`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET / PATCH | `/api/platform` | Read / update platform + provider config |
| POST | `/api/platform/github/connect` · `/disconnect` | GitHub connection state |
| POST | `/api/platform/github/create-issue` | Mirror an item as an issue (local; honest stub) |
| POST | `/api/platform/gdrive/connect` · `/disconnect` | Drive connection state |
| PATCH | `/api/projects/{id}` | Update project config |
| GET | `/api/projects/{id}/members` | List members |
| GET / POST / DELETE | `/api/api-keys` … | Manage API keys |
| GET | `/api/auth/me/memberships` | The current user's project access (Profile) |
