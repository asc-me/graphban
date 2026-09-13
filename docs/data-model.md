# Data model

SQLAlchemy models live in `backend/app/models/__init__.py`. The Postgres schema is owned by
Alembic; SQLite (tests / zero-infra dev) uses `create_all`.

## Entities

| Table | Key | Purpose |
| --- | --- | --- |
| `users` | `id` (`u1`, `u_…`) | Account: name, handle, email, avatar, initials, password hash |
| `projects` | `id` (`core`) | Project: name, **`tag`** (unique, 2–4 chars), accent, visibility, description, flags (`share_global_memory`, `auto_extract`, `mcp_enabled`, `embed_model`). Gitops overlay: `gitops_base_branch`, `gitops_no_push_to_base`, `gitops_branch_name_pattern`, `gitops_pr_title_pattern`, `gitops_reviewer_bar`, `gitops_version_scheme`, `gitops_release_defined_in` (NULL = inherit; **not** on `ProjectOut`). **`fleet_policy`** JSON nullable (PRD-37 D4): `{local_only, reviewer_cross_vendor, allowed_harnesses}`, a FILTER the supervisor applies before scoring any preference; NULL = no constraint, and a policy saved with every constraint off is stored as NULL rather than an empty rule. Migration `0110` |
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
| `agent_calls` | `id` | The Observe Live feed (PRD-34): one row per MCP call an agent made, reads and refusals included, attributed to `agent_id` — **NULL when the call could not be attributed** (no `agent_id` argument, no single live agent on the connection), which the board counts per credential rather than guessing. `source` (`observed\|reported`), `tool`, one `target` string from a per-tool allowlist (never the arguments), `ok`/`error_code`/`duration_ms`; `status`/`files` only on `reported` rows. Telemetry, not the audit ledger — Activity stays mutations-only and forever; this is swept by `AGENT_CALL_RETENTION_DAYS` on the write path. Migration `0102` |
| `delegations` | `id` | Delegation as a ledger fact (PRD-35): one row per `delegate` call, written BEFORE the child exists. `delegated_by`, `item_id`, `lane`, `requested_tier` (what the planner typed); `agent_id` + `linked_by` (`parent\|seat`) + `declared_model`/`declared_tier` once a declared child claims the item — a mismatch with the request is shown, never refused; `outcome` (`signed_off\|bounced\|blocked\|released`) and `closed_reason` (`withdrawn\|superseded`) are **stored at the event**, `open`/`claimed`/`expired` are derived from the row and the clock; `lease_seconds` is copied at write so expiry never moves. **`expired` is the third state**: a spawn that never claimed. Migration `0104` |
| `fleet_profiles` | `id` | A user's harness taste (PRD-37 D3 / PRD-41 D20 / GRPH-865): `user_id`, `project_id` (NULL = the user's default; set = an override for that project; unique per pair), `defaults` (ordered ALLOWLIST of harness names — empty means every matrix row), `weights` (`cost`/`quality`/`latency`/`locality`, each 0–1, normalised by the reader; 0 is indifference, never exclusion), `excludes` (harness or `harness:model` ids never to use), `budget_tokens` (nullable soft per-sign-off target; null means rank-scaling), `mix` (nullable harness→share map of recent launches; null is winner-take-all; never a filter), `updated_at`. Only preference lives here: facts are the supervisor's committed matrix, rules are `projects.fleet_policy` (which may include `caps`). Served on `fleet_status` and the brief as the KEY OWNER's profile (D14); `brief.mix` is the project histogram the resolver ranks against. Edited in the Fleet view. Migrations `0110`, `0120`, `0124` |
| `tracker_links` | `id` | A link between an external tracker (Linear first, Jira in v3) and an AgentLedger project, scoped to the org (PRD-10 / GRPH-186). `org_id`, `project_id`, `tracker_kind` (`linear`), `tracker_team_id` (external team UUID), `tracker_team_name` (cached display name), `authority` (tracker is authoritative — default true), `field_mapping` (per-link lossy field map, JSON), `write_back_comment` (Linear comment on assignee/status writes, on by default with per-link opt-out). Unique on `(org_id, tracker_kind, tracker_team_id)`. Migration `0125` |
| `sync_fingerprints` | `id` | Outbound write fingerprint for echo suppression (PRD-10 / GRPH-188). `link_id`, `issue_id`, `field`, `expected_version`, `write_token` (deterministic SHA-256 of the components), `consumed` (matched and dropped). When the hub writes back to the tracker, it records a fingerprint; when the webhook echoes that change back, the fingerprint matches and the hub drops it. Do NOT confuse with `idempotency_keys` — that maps create-tool retries; this maps outbound tracker writes. Indexed on `(link_id, issue_id, field)`. Migration `0126` |
| `tracker_mirror` | `issue_id` | Mirrored issue state from the external tracker (PRD-10 / GRPH-188). `link_id`, `tracker_kind`, `identifier`, `title`, `description` (nullable), `canonical_status`, `assignee_id` (nullable), `assignee_name`, `labels` (JSON array), `tracker_updated_at`, `version`, `url`, `mirrored_at`. The hub stores the latest snapshot; reconcile diffs incoming state against this to detect external edits. Tracker-owned fields apply on the hub immediately; AgentLedger-only fields (local links, provenance, memory) are never overwritten. Migration `0126` |
| `attempt_telemetry` | `id` | One FINISHED delegation, in the terms a comparison needs (PRD-38 D1): the work (`lane`, `task_class`, `size_band`, `attempt_no`), the runner (`vendor`, `model`, `binary_version`), the ending (`outcome`, `bounce_category`, `claim_to_finish_s`), the runtime facts only the supervisor sees (`turns_used`, `turn_budget`, `wall_seconds`, `tokens_in`/`tokens_out`, `exit_meaning`), and **how it was sampled** — `first_choice\|fallback\|explicit\|unknown`, derived from the supervisor's launch post (`chosen_winner`, `chosen_runner_up`, `chosen_source`). Unique on `delegation_id` AND on `enrolment_id`, one of which is always set: the launch post arrives before any delegation is linked. `derived_at`/`reported_at` say which half has landed, so an unreported number reads as "not reported" rather than as zero. `declaration_mismatch` flags a child whose declared vendor is not the one launched — recorded, never resolved. `branch_published_at` is when the supervisor reported putting the attempt's branch on the remote — until it does, `claim_review` withholds the item, because an item is not reviewable until its work is reachable (GRPH-754, migration `0114`). `resolution` holds PRD-37 D8's explanation as it stood at launch — the scored shortlist, every drop with the score it would have had, the profile that applied — because a recommendation's replay re-ranks THAT rather than simulating today's matrix over an attempt made under a different one (migration `0113`). Migration `0111` |
| `harness_rollups` | `project_id`+`week`+cell | One cell per ISO week (PRD-38 D11), recomputed from the raw rows rather than folded into: `median_seconds` is the median of the week's RAW seconds, tokens are SUMS over the attempts that reported with `tokens_reported` counting them, and no average is ever stored. `signed_off_reported` is the cost proxy's denominator — signed-off attempts AMONG those that reported — kept because the raw rows may be past retention by the time anyone reads the chart (migration `0112`). Weeks below the sample floor are written (the floor is a rule about reading a number, not about whether the week happened); weeks with no attempts are absent, and the chart draws that as a gap. Migration `0111` |
| `platform_rollups` | `week`+cell | The same cell across every org that opted in (PRD-38 D13, hosted only). Carries no org id and no project id; `orgs_contributing` and `top_org_share` are what the serving floor is checked against (≥3 orgs, n≥20, no org past 60%). An org counts toward `orgs_contributing` only with ≥5 of its own in the cell, so a dominant pair cannot be laundered by a third with one attempt — though its attempts still sum, since dropping them would bias the average toward whoever runs the most. Recomputed whole by `harness.platform_roll`, which is also the entire purge when an org opts out: nothing of theirs is stored to delete. Migration `0111` |
| `recommendation_marks` | `id` | A person has seen this recommendation card at this evidence (PRD-38 D7). `accepted_at` and `dismissed_at` share one row and one `evidence_hash` rule, because both mean the same thing to the page: stay quiet until the numbers move. Unique per user per card. Migration `0111` |
| `harness_lesson_marks` | `id` | This cell has crossed the sample floor once (PRD-38 D8). A mark, not a counter: `cell_key` omits the binary version, so a point release inherits it, and a cell that dips below the floor and comes back drafts nothing. Unique per project per cell. Migration `0111` |
| `harness_review_checks` | `id` | One later check on a review verdict (PRD-41 D6): `delegation_id` (the work reviewed), `reviewer_agent_id`, `verdict`, `kind` (`miss` \| `false_bounce` \| `confirmed` \| `withdrawn`), `unconfirmed` (a miss at bug filing, before the bug closes), `capabilities` of the work, `contradicted_by`. Rows rather than counters so a withdrawal recomputes F cells within 90 days. Unique per attempt × reviewer. Migration `0121` |
| `capability_probe_runs` | `id` | One operator-started probe panel (PRD-41 D7/D8): `source_project_id`, scratch `project_id`, `trigger` (`new_row` \| `version_change`), vendor/model/version, one `capability`, `item_ids`, `estimated_tokens`. Never started on a schedule. Migration `0121` |
| `capability_priors` | vendor+model+version+capability+band | The platform snapshot this instance last fetched (PRD-41 D12). Replaced whole on each fetch. `source=platform`, `n_band` (never a count), `snapshot_at`. Migration `0123` |
| `capability_snapshots` | `snapshot_at` | The hosted published prior for one date: served aggregate, `n` as a band, no identifier. Migration `0123` |
| `platform_contributions` | `id` | One accept of a self-hosted instance's rollups (PRD-41 D11). `instance_id`, `row_count`, `floors`, `opted_out`. Migration `0123` |
| `platform_contribution_cells` | instance+week+cell | The contributed cells, already redacted (`model=other` until three instances). Migration `0123` |
| `platform_model_sightings` | vendor+hash+instance | Accept-time redaction bookkeeping so the third sighting can un-redact forward only (criterion 29). Migration `0123` |
| `enrolments` | `id` | A SEAT (PRD-19): a single-use code, hashed, that grants one agent one role for one session — `role`, `wave`, `issued_by` (a human) or `minted_by` (a planner, via `mint_enrolment` / `delegate(seat=true)`), `expires_at` (30 min), `consumed_at`/`consumed_by`, `reissued_from` (the dead seat this one replaces), `revoked`. **Bound seat** (PRD-36): `item_id` and `delegation_id`, both nullable — registering on a bound seat claims that item for the new agent and links the delegation; an unbound seat is today's seat. Worker-role only when bound. Migrations `0066`, `0107` |

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
- **Gitops** — nullable process columns plus nullable `gitops_model` on **both**
  `organizations` (house process) and `projects` (overlay). NULL is unmeasured (org) or
  inherit (project). Sparse fields **are** inheritance; there is no extra toggle. The
  boolean is three-state: NULL is not `false`. `gitops_release_defined_in` is a path or
  URL of this repo's cut process (`get_context.gitops.release_defined_in`); unmeasured
  is not `docs/release.md`, and it is not a product CalVer. `gitops_model` is the last
  preset applied (`push_to_base` / `prs_to_base` / `prs_to_integration`), not a live
  field `get_context` emits. Presets never write `base_branch` or
  `release_defined_in`. Columns, not a JSON blob, so a missing key cannot look like
  “no requirements”. Not part of `ProjectOut` — that would serve a linked box's local
  `test` as live on `GET /api/projects`. Migrations `0095` (fields), `0096`
  (`gitops_model`), `0100` (`gitops_release_defined_in`).

## Migrations

```
0001 initial      users, projects, memberships, items, memory_shards, requests, links, api_keys
                  (+ CREATE EXTENSION vector + ivfflat index)
0002 prds         prds, prd_versions
0003 roadmap_mcp  milestones, mcp_tool_stats
0004 platform     platform_config
```
