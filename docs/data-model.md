# Data model

SQLAlchemy models live in `backend/app/models/__init__.py`. The Postgres schema is owned by
Alembic; SQLite (tests / zero-infra dev) uses `create_all`.

## Entities

| Table | Key | Purpose |
| --- | --- | --- |
| `users` | `id` (`u1`, `u_…`) | Account: name, handle, email, avatar, initials, password hash |
| `projects` | `id` (`core`) | Project: name, **`tag`** (unique, 2–4 chars), accent, visibility, description, flags (`share_global_memory`, `auto_extract`, `mcp_enabled`, `embed_model`). Gitops overlay: `gitops_base_branch`, `gitops_no_push_to_base`, `gitops_branch_name_pattern`, `gitops_pr_title_pattern`, `gitops_reviewer_bar`, `gitops_version_scheme` (NULL = inherit; **not** on `ProjectOut`) |
| `organizations` | `id` | Hosted tenant. House gitops columns match the project overlay set (NULL = unmeasured) |
| `password_resets` | `id` (`pwr_…`) | A single-use way back into an account: **`token_hash`** (sha256 — the plaintext exists only in the email), `expires_at`, `used_at` set on success, `requested_ip` for provenance |
| `memberships` | `id` | User ↔ project with `role` (owner/admin/member) + `access` (write/read/none) |
| `items` | `id` (frozen at issue) | Tracker item: **`number`** (unique per project), title, description, `status`, tags, effort, `sort_order`, blocker, reporter, `pr` (JSON), date |
| `memory_shards` | `id` (`m1`, `m_…`) | Shard: text, `scope`, `reach` (`project\|org`), `lesson_class`, source, optional `item_id`, `embedding` (vector), `fresh`. Attribution (`actor_user_id`, `attributed_project_id`) is NULL until measured |
| `lesson_outcomes` | `id` | Counted evidence on a published shard (`caught\|missed\|applied\|contradicted`). Effectiveness reads this list; empty is unknown, not 1.0 |
| `requests` | `id` (frozen at issue) | Triage: **`number`**, type, title, by, votes, status, `linked_to` |
| `links` | `id` | Typed edge: `a`, `b`, `type` (dependency/code/semantic/tag), `confidence`, `reason` |
| `prds` | `id` (frozen at issue) | PRD: **`number`**, title, status, version, body (markdown), `linked` (item ids), updated |
| `prd_versions` | `id` | Immutable snapshot: `prd_id`, version, date, note, body |
| `milestones` | `id` | Roadmap entry: `phase` (mvp/post/later), title, tag, `done`, `sort_order` |
| `mcp_tool_stats` | `tool` | Per-tool MCP call count |
| `platform_config` | `project_id` | Per-project LLM mode + provider config + GitHub/Drive connection state |
| `credentials` | `id` | One LLM provider credential, owned by the deployment (`org_id` NULL) or by an org. Keyed by ROW, so two keys for the same provider are two rows (PRD-25 D-a) |
| `deployment_config` | `scope` | The default / fallback / embedding credential for one scope. `scope` is `''` for the deployment itself, an org id under hosted multi-tenancy |
| `reindex_progress` | `scope` + `table_name` | How far a re-index has got, PER TABLE (PRD-25 S4b). One counter cannot distinguish "finished memory_shards" from "partway through it", so a resume would have to choose between redoing finished work and skipping unfinished work |
| `api_keys` | `id` | Scoped agent key: name, prefix, `hashed_key` (SHA-256), scopes, last used |
| `project_tag_history` | `tag` | A tag a project used to hold — one row per rename. Tags are never reused on a deployment |
| `legacy_entity_keys` | `old_key` | Ids issued before project tags existed (`AL-12`, `R-33`, `PRD-1`), seeded once so they resolve forever |
| `llm_call_spans` | `id` | One row per provider call (GRPH-225): provider/model/base_url, `kind` (`chat\|extract\|embed\|tool_turn`), `feature`, `project_id` (a plain string, NOT a foreign key — a span must outlive the project it was billed to), token counts with `tokens_source` (`reported\|estimated\|none`), `cost_usd` (**NULL = unpriced**, never 0), latency, and `ok`/`error_class`/`http_status`/`retryable`. `output_preview` (nullable, 512 chars) is a truncated model reply for human-eval sampling (GRPH-644) — NULL means nothing to label, never `""`; the prompt is not stored. Retention: `LLM_SPAN_RETENTION_DAYS`, swept at startup. Migrations `0097`–`0098` |

## Keys are rendered, not stored (PRD-13)

A user-visible key — `GRPH-12`, `GRPH-R33`, `GRPH-P4` — is **rendered** from the
project's current `tag`, the entity kind, and the entity's `number`. The stored `id` is
frozen when the entity is created and is never rewritten, so changing a tag is one
`UPDATE` on one row and nothing else in the database moves.

That matters because twelve columns across ten tables hold an entity id and only three
are enforced foreign keys. `app/tagging.py` owns the grammar; `services/keys.py`
resolves a supplied key back to a stored id (current form → tag history → legacy table →
the id itself) and mints new ones; `services/projects.retag_project` moves a tag.

Retagging is therefore one `UPDATE` plus one `project_tag_history` row, committed
together — a tag that moved without its history row would silently break every key ever
rendered under the old one. Tags are never reusable on a deployment.

## Relationships

```
users ─< memberships >─ projects
projects ─< items, requests, links, prds, milestones, memory_shards, platform_config
items ─< memory_shards (item_id)          # item-scoped shards / lessons
items <─ requests (linked_to)             # a request linked to an item
items <─ prds.linked (id list, JSON)      # PRD ↔ items
prds  ─< prd_versions
users ─< api_keys
```

## Notes

- **Embeddings** — `memory_shards.embedding` is a real pgvector `vector(EMBED_DIM)` on
  Postgres (with an ivfflat cosine index) and JSON on SQLite, via a dialect-aware
  `EmbeddingType`. `EMBED_DIM` must match the [embedding provider](ai-providers.md).
- **Human ids** — items (`AL-<n>`), requests (`R-<n>`), and PRDs (`PRD-<n>`) use readable
  ids computed from the max existing number.
- **PRD versions** — the latest snapshot stores the full body; older seeded snapshots keep
  their note/date only. New snapshots (via the editor) always store the body.
- **Links** — `a`/`b` are plain id strings (items or requests), not foreign keys, so an edge
  can span either kind.
- **Gitops** — six nullable process columns plus nullable `gitops_model` on **both**
  `organizations` (house process) and `projects` (overlay). NULL is unmeasured (org) or
  inherit (project). Sparse fields **are** inheritance; there is no extra toggle. The
  boolean is three-state: NULL is not `false`. `gitops_model` is the last preset applied
  (`push_to_base` / `prs_to_base` / `prs_to_integration`), not a seventh live field
  `get_context` emits. Columns, not a JSON blob, so a missing key cannot look like “no
  requirements”. Not part of `ProjectOut` — that would serve a linked box's local `test`
  as live on `GET /api/projects`. Migrations `0095` (six fields), `0096` (`gitops_model`).

## Migrations

```
0001 initial      users, projects, memberships, items, memory_shards, requests, links, api_keys
                  (+ CREATE EXTENSION vector + ivfflat index)
0002 prds         prds, prd_versions
0003 roadmap_mcp  milestones, mcp_tool_stats
0004 platform     platform_config
```
