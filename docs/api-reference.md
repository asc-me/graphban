# API reference

All endpoints are under `/api` (proxied by the web tier and served directly by the API).
Interactive OpenAPI docs are at **`/docs`**.

**Auth legend:** **JWT** = `Authorization: Bearer <access-jwt>` · **MCP** = API key via
`X-API-Key` / `Authorization: Bearer al_sk_…` · **public** = no auth (rate-limited).

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

The two API-key routes are the odd ones out and for the same reason: their callers are cron
and a generated hook running on somebody's machine, neither of which can hold a session.
The equivalent of the run endpoint for a local Docker install is
`docker compose exec api graphban learn run --stage ingest`, which calls the same service
function.

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
