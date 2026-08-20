# API reference

All endpoints are under `/api` (proxied by the web tier and served directly by the API).
Interactive OpenAPI docs are at **`/docs`**.

**Auth legend:** **JWT** = `Authorization: Bearer <access-jwt>` · **MCP** = API key via
`X-API-Key` / `Authorization: Bearer gb_sk_…` · **public** = no auth (rate-limited).

## Health

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/health` | none |

## Auth

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/auth/login` | none | Email + password → access + refresh tokens |
| POST | `/api/auth/register` | none | Create a user → tokens |
| POST | `/api/auth/refresh` | none | Refresh token → new tokens |
| GET | `/api/auth/me` | JWT | Current user |
| GET | `/api/auth/me/memberships` | JWT | Current user's project access |

## Projects

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/projects` | JWT | List projects (each carries its `tag`) |
| POST | `/api/projects` | JWT | Create a project. `tag` is optional — derived from `name` when omitted, refused with 422 when taken or malformed |
| GET | `/api/projects/tag-suggestion?name=` | JWT | A free tag derived from a project name (prefills the creation form) |
| GET | `/api/projects/tag-check?tag=` | JWT | `{tag, available, reason}` for live form feedback |
| POST | `/api/projects/{id}/retag` | JWT (write) | Move the project's tag. One UPDATE + one tag-history row + one audit event; **no other row in the database changes**. Retired tags are never reusable |
| PATCH | `/api/projects/{id}` | JWT | Update project config. **Not** the tag — changing that has to record tag history |
| GET | `/api/projects/{id}/members` | JWT | List members (role/access) |

## Items (tracker)

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/api/items` (`?status=`, `?project_id=`) | JWT |
| POST | `/api/items` | JWT |
| PATCH | `/api/items/reorder` | JWT |
| GET | `/api/items/{id}` | JWT |
| PATCH | `/api/items/{id}` | JWT |

## Requests

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/api/requests` (`?type=`) | JWT |
| POST | `/api/requests` | JWT |
| POST | `/api/requests/{id}/vote` | JWT |
| POST | `/api/requests/{id}/link` | JWT |

## Memory & agent chat

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/memory/shards` | JWT | (optional `?status=candidate\|published\|rejected`) |
| GET | `/api/memory/candidates` | JWT | The review queue — agent shards awaiting publish (AL-49) |
| POST | `/api/memory/shards` | JWT | Human-authored → published immediately |
| PATCH | `/api/memory/shards/{id}` | JWT | |
| POST | `/api/memory/shards/{id}/publish` | JWT | Promote a candidate into the trusted retrieval path |
| POST | `/api/memory/shards/{id}/reject` | JWT | Reject a candidate (kept for provenance, never searched) |
| POST | `/api/memory/search` | JWT | Published-only semantic search |
| POST | `/api/memory/backfill` | JWT |
| GET | `/api/memory/export` | JWT |
| POST | `/api/memory/import` | JWT |
| POST | `/api/agent/chat` | JWT |
| POST | `/api/agent/chat/stream` | JWT (SSE) |
| POST | `/api/agent/code` | JWT |
| POST | `/api/agent/code/stream` | JWT (SSE) |
| GET | `/api/agent/code/map` | JWT |
| GET | `/api/agent/code/analysis` | JWT — hubs, components, and optionally a path (PRD-20 D8) |
| GET | `/api/agent/code/health` | JWT — is the graph still true: coverage, stale nodes open work still claims, touchpoints resolving to nothing. Retires nothing; `ever_described` distinguishes "nothing stale" from "nothing described" |
| GET | `/api/agent/code/neighbors` | JWT |
| GET | `/api/agent/code/for` | JWT — code linked to an item/request (work→code) |
| POST | `/api/agent/code/link` | JWT — link an item/request to a code path |
| POST | `/api/agent/code/unlink` | JWT |

`/api/agent/code` is the code-graph consumer: it grounds the configured ChatModel in the
code structure the coding agent described via MCP (`search_code` + `code_neighbors`), so the
connected LLM can answer "what depends on X" from real edges — never from a checkout it
doesn't have. Returns `{reply, nodes:[{node, score}]}`; the `/stream` variant emits a `nodes`
SSE event, then `delta`s, then `done`. Body is `{message, project_id?}`.

## Learning loop (PRD-16)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/api/learning/run` | **API key** | Drive the loop. `{stage, project_id?, limit_sources?}` (GRPH-353) |
| GET | `/api/artifacts/recommendations` | JWT | The review queue — superseded rows excluded |
| GET | `/api/artifacts/recommendations/{id}` | JWT | One recommendation, its draft, and its install plan |
| POST | `/api/artifacts/recommendations/{id}/review` | JWT | `{decision: approve\|reject}` — the human boundary |
| GET | `/api/artifacts/usage` | JWT | Population + uses; `uses` is `null`, never `0`, for an unmeasurable tier |
| GET | `/api/artifacts/stale` | JWT | Measurable artifacts with no observed use in 30 days |
| POST | `/api/artifacts/inventory` | **API key** | A client reporting what is installed on its machine (GRPH-354) |
| GET | `/api/artifacts/inventory` | JWT | What is installed, with `forked` and `orphaned` named |
| POST | `/api/artifacts/{id}/used` | **API key** | A generated hook reporting that it fired |

`stage` is `ingest`, `artifacts`, or `all`. The two stages sit either side of **human
triage** and that is the design, not a limitation: ingest writes `candidate` shards,
classification reads `published` ones. So a single pass on a fresh install does nothing
downstream, and the artifact stage picks up whatever was approved since the last run —
however long ago that was. An unknown stage is a 422 rather than a silent run of everything.

Both run stages are safe to re-run and cheap when nothing changed: the ingest watermark only
advances after events are written, classification skips lessons already classified, and
drafting is keyed on a hash of the lesson text. A second pass over unchanged input makes
**zero** provider calls.

The API-key routes are the odd ones out and for the same reason: their callers are cron, a
generated hook, or a scanner running on somebody's machine, none of which can hold a session.
The equivalent of the run endpoint for a local Docker install is
`docker compose exec api graphban learn run --stage ingest`, which calls the same service
function.

### The inventory is a client-side scan

`usage_report` used to read only `artifact_recommendations` — artifacts *this pipeline
generated* — so every skill, hook, agent and rule a human wrote by hand was invisible, and a
fresh install reported a population of zero while the operator's `.claude/` directory held
dozens. The scan therefore runs where the files are and posts its findings up:

    graphban learn inventory --root ~/.claude --api-url https://cloud.example --api-key gb_sk_…

A *server-side* walk would find nothing under `hosted_mode` and nothing inside the compose
container either — and would report zero without erroring. Three properties are load-bearing:

- **Read-only.** Nothing on the scanned machine is written, moved or deleted, under any input.
- **A discovered artifact is never measurable**, whatever its tier. Graphban meters its own
  MCP calls and instruments the hooks it renders; it has no instrumentation inside a
  hand-written skill, so `uses` stays `null`. Discovered artifacts never become stale and can
  never be retired.
- **Orphaning is scoped to the `root` posted.** A scan of `~/.claude` says nothing about
  `~/work/.cursor`; an artifact missing from a scan that never looked for it is flagged
  `orphaned`, never deleted.

`forked` is the state that matters most: a *generated* artifact whose contents on disk no
longer match what was rendered has been edited by a human, and `install_plan` refuses it.
Updates are full re-renders, so writing would silently discard that edit — the exact trust
failure the propose-only boundary exists to prevent.

## Fleet (PRD-17)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/fleet` | JWT | Roster + review queue + cluster board in one read |
| POST | `/api/fleet/keys` | JWT | Mint a credential narrowed to one role and tagged to a wave |
| GET | `/api/fleet/end-wave` | JWT | What ending the wave would destroy, for the confirm |
| POST | `/api/fleet/end-wave` | JWT | Revoke the wave's keys and release everything they hold |

Agent presence is **derived from last contact**, never stored — an agent that dies doesn't
report it, so a stored status would read healthy for a process killed an hour ago. A revoked
credential reads `offline` immediately rather than waiting out the TTL, and *without* backdating
`last_seen_at`: the agent really was seen when it says: what changed is that its key no longer
works.

Only keys carrying a `fleet_wave` tag are swept by **End wave**. A hand-minted credential is
somebody's long-lived key, and revoking it would be a surprise that button never promised.

## PRDs

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET / POST | `/api/prds` | JWT |
| GET / PATCH | `/api/prds/{id}` | JWT |
| GET / POST | `/api/prds/{id}/versions` | JWT |
| POST | `/api/prds/{id}/link` | JWT |
| POST | `/api/prds/{id}/ai` | JWT | one-shot AI command (expand/risks/summarize/grill) |
| POST | `/api/prds/{id}/grill/stream` | JWT (SSE) | interactive grill — clarifying questions (AL-67) |
| POST | `/api/prds/{id}/grill/apply` | JWT | fold grill decisions into a proposed PRD body |

## Analytics

| Method | Path | Auth | Returns |
| --- | --- | --- | --- |
| GET | `/api/dashboard` | JWT | Aggregated project health |
| GET | `/api/roadmap` | JWT | Phases + milestones + progress |
| GET | `/api/links` | JWT | Typed links |
| GET | `/api/mcp/tools` | JWT | Tool schemas + live call counts |

## Platform & integrations

| Method | Path | Auth |
| --- | --- | --- |
| GET / PATCH | `/api/platform` | JWT |
| POST | `/api/platform/github/connect` · `/disconnect` · `/create-issue` | JWT |
| POST | `/api/platform/gdrive/connect` · `/disconnect` | JWT |

## Galaxy (hosted only, PRD-21 D3)

Repo-level dependencies inside one org. Every edge names the file that proves it — the
whole difference between this graph and a guess. Nothing is inferred from similarity.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/orgs/{id}/galaxy` | Nodes (projects), edges (typed + evidenced), and name collisions |

Edges arrive on the code-graph push (`POST /api/sync/code-graph`), which gained two
optional fields. The client sends **facts, not edges**: it cannot know what else exists in
the org, so the server resolves each declared name against `Project.provides`.

- `provides` — package names this project publishes.
- `manifests` — `[{"name": "@acme/core", "evidence": [{"file": …, "fact": …}]}]`.

Three rules the wire format encodes, each of which would be a permanent defect if wrong:

- **Omitted `manifests` ≠ empty.** Omitted means an older client did not look, and stales
  nothing. Present-but-empty means it looked and found none, and stales that project's
  edges. Collapsing them would make every old client silently delete a dependency graph.
- **Evidence is required** — 422 without it.
- **Unresolved names are dropped but counted**, and reported in the push response. A name
  two projects both claim resolves to neither and is surfaced as a collision.

Stale edges are marked, never deleted, and their evidence is never trimmed: a relationship
with no explanation is worse than a deleted one. Purging is an explicit operator action.
## Linked deployments (hosted only, PRD-21 D6)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/orgs/{id}/deployments` | The local boxes pushing into this tenant |

**Nothing here reaches into a box.** Every field is already cloud-held: a linked deployment
runs only the code-graph tools locally and forwards claims, leases and heartbeats, so
"which agents are running, on what" is a query rather than an embed. Relay and reverse
tunnel are *rejected, not deferred* — the box pushes, the cloud never reaches in.

**One key is one deployment.** The cloud stores no other deployment identity, so the sync
credential's name is the label everywhere, which makes naming it at mint time load-bearing.

`POST /api/sync/code-graph` gained an optional `base_url`: where the box says it can be
reached. It is **a hint, never a guarantee** — the same machine answers at different
addresses from different networks, and the cloud makes no attempt to verify it (a
cross-origin probe from the console would hang or be blocked). The UI renders it as text
and then links it, with a per-user override in `localStorage`.

Two distinctions the payload carries:

- **`never` is not `stale`.** A credential that has never pushed is a link somebody set up
  and did not finish; one that pushed a month ago is a box that stopped. Different actions.
- **A revoked credential stays listed**, marked. A retired deployment is history, not
  noise. (Gap: `DELETE /api/api-keys/{id}` hard-deletes, so a credential retired that way
  does vanish; only the soft revoke used by the fleet sweeps keeps the row.)

## Teams (hosted only, PRD-21 D5)

A team is a named group in an org with **grants** — a project plus an access level — and a
grant is the unit of access administration.

| Method | Path | Purpose |
| --- | --- | --- |
| GET / POST | `/api/orgs/{id}/teams` | List or create teams |
| DELETE | `/api/teams/{id}` | Disband, recomputing what it granted |
| POST / DELETE | `/api/teams/{id}/members/{user_id}` | Add or remove a member |
| PUT / DELETE | `/api/teams/{id}/grants/{project_id}` | Set or revoke a grant |

**A grant materializes.** It writes real `Membership` rows rather than adding a resolution
step to `authz.can_read` / `can_write`. Those two are the hottest authorization path in the
application and every route depends on them, so resolving team closure at read time would
change the risk profile of the whole app for a feature that is administrative. Materialized,
the blast radius is `teams.recompute` and its tests, and every existing authz test keeps its
meaning.

Four rules follow, and each is a wrong answer avoided:

- **Authorization never reads `origin` — it reads `access`.** `origin` exists only so D8 can
  refuse a direct edit on a derived row, and so revocation knows what it may recompute.
- **Revocation recomputes** from the grants that remain, in the same transaction. It does not
  delete rows attributable to the revoked team: a second team still granting the project keeps
  the row, and bookkeeping that removed it would strip access someone legitimately has.
- **A direct membership always survives and wins.** Where direct and derived collide the grant
  materializes nothing — bulk administration does not overwrite a human's explicit decision.
- **Access resolves to the highest** across all sources, so nobody is less able because of the
  order two administrators happened to act in.

A derived membership refuses a direct edit through `PUT /api/projects/{id}/members/{user_id}`
(409, naming the team). That is the drift materializing costs, made visible rather than silent:
an edit there would be undone by the next recompute.

## Membership mutations (hosted only, PRD-21 D8)

Closes the governance gap in §3.5: before these, members arrived by accepting an invite
and stayed forever at the role it carried. Every one is an **authority action** and lands
in the event ledger.

| Method | Path | Purpose |
| --- | --- | --- |
| PATCH | `/api/orgs/{id}/members/{user_id}` | Change org role (`admin` / `member`) |
| DELETE | `/api/orgs/{id}/members/{user_id}` | Remove from the org, cascading project access |
| PUT | `/api/projects/{id}/members/{user_id}` | Set project access (`write` / `read` / `none`) |

Four refusals, each a rule rather than a precaution:

- **The owner cannot be demoted or removed**, and ownership is not grantable. An org that
  can lose its last owner is one nobody can administer.
- **Nobody edits themselves.** An admin who can grant themselves owner is not an admin.
- **Nobody grants a rank above their own.**
- **Project access needs an org seat first** — access inside an org you do not belong to
  would be a path with no roster entry, invisible on every screen that lists who is here.

`DELETE` returns `{removed_role, projects_revoked}` rather than a bare success, so the
caller can say what was lost. A removal that silently left project access behind is the
worst kind of quiet: gone from the roster, still able to reach the work. `none` is a
**stored** access level, not a deletion — an explicit "not this project" is a decision
somebody made, and should not read the same as never having been considered.

## Operator plane (hosted only)

Cross-tenant, and gated twice: `HOSTED_MODE` must be on **and** the caller must be in
`PLATFORM_ADMIN_EMAILS`. Every route 404s otherwise — not 403 — so the plane's existence
is never disclosed to a tenant. Returns **metadata only**: orgs, plans, usage, identity,
invites. No tenant content (items, memory, PRDs, requests, code graph) is reachable here,
which is what keeps the cross-tenant isolation guarantee honest.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/admin/me` | Operator probe; also reports `signup_mode` + `invite_expiry_days` |
| GET | `/api/admin/orgs` | Every tenant: owner, plan, usage vs limits, members |
| GET | `/api/admin/users` | Every account: identity, org memberships, `last_write_at` |
| GET | `/api/admin/invites` | Outstanding platform invites; `?history=true` for all ever issued |
| POST | `/api/admin/invites` | Issue a platform invite (new customer founds their own org) |
| DELETE | `/api/admin/invites/{id}` | Revoke a **pending** invite |
| GET | `/api/admin/activity` | Operator ledger — actions taken from this plane, newest first |
| GET | `/api/admin/org-requests` | Pending additional-org requests |
| POST | `/api/admin/org-requests/{id}` | Approve or deny one |

Plan assignment is `PUT /api/orgs/{id}/plan`, operator-gated the same way. There is no
suspend, restore, or impersonate — those endpoints do not exist.

Two response fields carry a distinction the UI depends on:

- **`last_write_at: null`** means "no write on record", not "inactive". Reads are never
  evented, so a user who signs in daily and only reads is indistinguishable in the data
  from one who never returned. The console renders it as its own state.
- **`redeemed_org_name: ""`** on an accepted invite means the account it seeded exists but
  has not founded an org yet — a different fact from a pending invite.

## API keys

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/api/api-keys` | JWT |
| POST | `/api/api-keys` | JWT (plaintext returned once) |
| DELETE | `/api/api-keys/{id}` | JWT |

## Reports (upstream feedback)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/reports/upstream` | JWT | Whether upstream reporting is on + where reports go |
| POST | `/api/reports/upstream` | JWT | Forward a user-initiated Graphban issue report upstream |

## MCP

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/mcp` | MCP | JSON-RPC 2.0 — `initialize` / `tools/list` / `tools/call` |

See [MCP tools](mcp.md) for the tool catalog.

## Public (unauthenticated)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/public/requests` | Submit feedback + return duplicates |
| GET | `/api/public/duplicates` | Live duplicate check (`?q=&project_id=`) |
| GET | `/api/public/roadmap` | Read-only roadmap (for the share link) |
| POST | `/api/public/github/webhook` | Inbound GitHub issue → tracker item |

All public endpoints share a per-IP sliding-window rate limit (20/60s).
