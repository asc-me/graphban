# Project sync wire contract (v1)

The box is home for every project it syncs (GRPH-P89 D1). The console holds a read
replica. Cloud edits come back as proposals the box applies. This is the contract
both sides build against (GRPH-1732, console PRD GBC-P19). The box client is
`backend/app/services/project_sync_client.py`. Tests use the in-process console
`backend/tests/project_sync_fake.py`.

Every call is under the linked console URL. The credential is the one
`code_sync._console_auth` already resolves: a deployment token as
`Authorization: Bearer`, or a migrated key as `X-API-Key`. Project-scoped calls
also send `X-Cloud-Project` with the cloud project id. The token is never a
response field and is never logged.

Every push sends `X-Sync-Contract: 1`. An unknown major is
`422 {detail: "contract", field}` naming the header or the body field.

Each call returns one of three states:

| State | When |
| --- | --- |
| `ok` | The console answered the call. A `304` is `ok` with `not_modified`. |
| `refused` | A definite no. `reason` is the console's `detail`. |
| `unknown` | No answer: not linked, network error, timeout, `408`, `425`, `429`, or `5xx`. Never read as a refusal. |

A `429` is `unknown` and carries `retry_after`, the `Retry-After` delta-seconds.
A per-entity `refused` list on a `200` is not a refused call: the flusher parks
those entities and the rest of the batch counts as stored.

## `GET /api/sync/contract`

No body. `200 {version: "1"}`. One global version (D9), read once per cycle.

## `POST /api/sync/projects`

Request: `{tag, name}`.

| Answer | Body |
| --- | --- |
| `201` | `{cloud_project_id, tag}` — a new replica. `adopted` is absent. |
| `200` | `{cloud_project_id, tag, adopted: true}` — the tag's previous home deployment is revoked and this deployment belongs to the same engineer. The id is the old replica, not a new one. |
| `409` | `{detail: "tag_taken", owner_label}` — the tag belongs to someone else. `owner_label` names that owner. |

## `POST /api/sync/project-entities`

Gzip body (`Content-Encoding: gzip`, `Content-Type: application/json`). Headers
`X-Cloud-Project: <cloud project id>` and `X-Sync-Max-Delay: <seconds>`. The
delay is the box's max flush delay. D11's default is 60.

Request:

```json
{"contract": "1", "entities": [
  {"type": "item|prd|comment|wait", "id": "…", "version": 1, "hash": "sha256:…", "body": {}}
]}
```

`200 {stored: [{type, id, version}], refused: [{type, id, version, reason}]}`.
`reason` is `too_large`, `invalid`, or `forbidden_type`. Idempotent by
`(type, id, version)`: a replay is `stored` again and does not create a second
row. The first body wins.

`too_large` is one entity whose canonical JSON (sorted keys, no extra
whitespace) exceeds 1 MiB, the D11 batch ceiling. One over-cap entity does not
refuse its neighbours.

| Answer | Body |
| --- | --- |
| `429` | `Retry-After` seconds. The call is `unknown`, not refused. |
| `422` | `{detail: "contract", field}` for an unknown major. |
| `403` | `{detail: "not_home"}` when this deployment is not the project's home. |

## `DELETE /api/sync/project-entities?project=<cloud project id>`

`200 {deleted}` — the count of entity versions removed. The console closes open
proposals for that project as `refused: project unsynced`. Also sends
`X-Cloud-Project`.

## `GET /api/sync/proposals?project=<cloud project id>&after=<cursor>`

Send `If-None-Match` when the caller holds an `ETag`.

| Answer | Body |
| --- | --- |
| `304` | Empty. Same `ETag`. An idle poll, including this one, is what keeps the console from showing the project as stale, so the poll keeps running while nothing has changed. The client reports `ok` / `not_modified` and does not invent a cursor. |
| `200` | `{cursor, proposals: [{id, kind, target, base_version, payload, proposer, created_at}]}` and an `ETag`. |

`kind` is `comment`, `answer`, `edit`, or `create`. `target` is `{type, id}` or
`null`. `proposer` is `{label, role}`. `after` is exclusive. A non-numeric
`after` reads as the start, not as an empty page.

## `POST /api/sync/proposals/outcomes`

Request: `{outcomes: [{id, status, reason?, current_version?}]}`. `status` is
`applied`, `refused`, or `conflict`.

| Answer | Meaning |
| --- | --- |
| `200` | Recorded. The same id and status again is a no-op, still `200`. |
| `409` | `{detail: "already_resolved"}` — a different status for an id already resolved. Nothing from that request is written. |

## Not a call

Unlink is not on this contract. The console treats a revoked deployment as
unlinked and closes its open proposals as `refused: box unlinked`. A relink
through device sign-in supersedes the old deployment and the console moves the
home; the box does not send a call for that. Adopting a tag whose home
deployment was revoked is the `POST /api/sync/projects` `200`, above, and only
when the new deployment is the same engineer.

## Client

| Call | Function |
| --- | --- |
| `GET /api/sync/contract` | `get_contract` |
| `POST /api/sync/projects` | `register_project` |
| `POST /api/sync/project-entities` | `push_entities` |
| `DELETE /api/sync/project-entities` | `purge_entities` |
| `GET /api/sync/proposals` | `pull_proposals` |
| `POST /api/sync/proposals/outcomes` | `report_outcomes` |

Each takes the box `Session` and an optional `transport` (the fake's
`transport()`). `push_entities` takes `max_delay` (default 60) and sends it as
`X-Sync-Max-Delay`.

## Fake

`FakeConsole` implements the answers above. `add_deployment(token, engineer=, label=)`
registers a credential. `revoke(token)` is the console-side unlink.
`enqueue_proposal` queues a proposal. `timeout_once`, `fail_once`,
`status_once`, and `retry_after_once` inject the failure the next call sees.
`max_entity_bytes` defaults to 1 MiB.

## Box apply (GRPH-1737)

`project_sync_proposals` pulls `GET /api/sync/proposals` for each project at
`sync_level=sync` that has a cloud id. The normal interval is 60s. While that
project has an open `wait:decision`, it is 10s, measured from the last poll.
`after_push(local_project_id)` marks the project due on the next pass, which
is how a push is followed immediately. The leader loop ticks every
`proposal_poll_seconds` (`0` disables it). The tick is not the interval.

Each proposal goes through the service layer. `cloud_permission` is enforced
on the box: `read_only` refuses every proposal, `comments` allows only
comment and answer, `proposals` allows all four. In every permission the box
also refuses, naming the rule: status `done`, `sign_off`, an attestation
waiver, closing a PRD, and rules changes. An edit whose `base_version` is not
the entity's current version is `conflict` and changes nothing.

The proposal id is stored in the same transaction as the apply. A re-delivery
reports the stored outcome and does not apply again. Outcomes go to
`POST /api/sync/proposals/outcomes`. A failed post stays queued (`reported`
false) and is retried; the apply is not repeated. The actor on the item
history is `cloud:<proposer.label>`, and the history records the proposal id.

## Box admin (GRPH-1739)

These routes are on the box. They are not the console's `POST /api/sync/projects`.
A box admin is required for every write. A non-admin is `403` with
`sync.link_needs_box_admin`, and the stored row is unchanged.

`GET /api/sync/projects` adds `local`: one object per local project. A project
with no `sync_project` row is `sync_level: local`. It is not reported as
digests, and it is not reported as synced. Each object carries `cloud_permission`,
`backfill` (`total`, `confirmed`, `parked`, `state`), `queued`, `parked`
(`count`, `reasons`), `backlog_bytes`, `held_by` (`budget`, `retry_after`,
`contract`, or null), `last_push_at`, and `last_push_result`. `unknown` is
returned as `unknown`. The same facts are copied onto each `mapping` row.
`held_by: retry_after` means this project still has an unparked row and the
box is inside a console `Retry-After` (a 429, a timeout, or a 5xx). A 429
records `last_push_result: unknown` and does not park the row.

`PUT /api/sync/projects/{id}` body is `{sync_level?, cloud_permission?,
create_in_org?, purge?, cloud_project_id?, cloud_project_tag?}`. `sync_level` is
`sync`, `digests`, or `local`. `create_in_org` with no stored cloud id calls the
console register. A `409` answers `{detail: tag_taken, owner_label}` and stores
no mapping. A project that is already mapped keeps its mapping: create-in-org
syncs to the cloud project it has, and a refusal changes nothing. `purge`
defaults to true when leaving `sync`.

`cloud_project_id` (with `sync_level` `sync` or `digests`, never with
`create_in_org`) maps this one project to an existing cloud project. No other
project's mapping changes, unlike `PUT /api/sync/projects`. A local-only project
needs it, or `create_in_org`, to leave local only: `digests` never creates a cloud
project. Re-pointing a project that syncs, or that still owes a purge, is `422`
— set it to local only first. The wizard's ③ and Settings → Sync both save one
project at a time this way.

`POST /api/sync/projects/{id}/retry-parked` clears this project's parked rows
and returns `{retried}`. Zero means the project was read and nothing was parked.

`GET /api/sync/budget` is any signed-in user: `{bytes_per_min, requests_per_min}`.
The env defaults (256 KiB/min and 30 requests/min) are what the process starts
with. `PUT /api/sync/budget` replaces them for this process. Zero is a ceiling.
Negative is `422`. A restart returns to the env.
