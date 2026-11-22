# PRD-89 project sync — acceptance walk (GRPH-1801)

A real box was walked against the real staging console. The fakes on either side were not used.

- **Box:** a fresh compose stack on this Mac, project `walk-sync` (tag `WALK`). It holds 13 items (WALK-1..13), PRD WALK-P1, and an open `wait:decision` on WALK-13. The walk ran on box 2026.11.46 and 2026.11.48, and finished on **2026.11.53** (0f1b82ea).
- **Console:** staging, `demo.graphban.dev`. It started on `v2026.11.20-rc.2` and finished on **`v2026.11.22-rc.1`** (7cda9d4).
- **Org:** Staging Smoke. The walking account owns it and is also a viewer in the demo org Graphban.
- **Dates:** 2026-11-21 to 2026-11-22.

The walk found five defects that no unit test had caught. Each was fixed before the clause it blocked was walked again:

| Defect | Side | Item | What the fake or the tests missed |
|---|---|---|---|
| Every entity refused `invalid`: the box versioned with a 63-bit content hash, but the console accepts only safe integers that rise | box | GRPH-1815 (#2052) | The box's fake console enforced neither rule |
| The box tag was lowercased (`WALK` → `walk`), so a clash with an existing uppercase tag was missed | console | GBC-264 (#300) | The console tests used lowercase tags |
| An owner who is also a viewer in another org was refused every `/api/projects/:id/*` write (`viewer_read_only`) | console | GBC-265 (#306) | No test held two roles in two orgs |
| Outcomes named no project, so the console answered 422 to every one | box | GRPH-1842 (#2136) | The box's fake never checked the project |
| Pulled proposals were read as nested `target`/`proposer`, but the console sends flat fields, so every target was "not found" | box | GRPH-1842 (#2136) | The box's fake served the nested shape |

Two defects are still open and were found by the walk. Neither blocks a clause:

- GRPH-1850: a project set to Local only reports `backfill: running 0/15` for ever.
- The P89 contract had no fleet-side defect, but the walk was delayed by fleet stalls. Those are filed as GRPH-1843, GRPH-1848 and GRPH-1849.

## Summary

| # | Acceptance clause | Verdict |
|---|---|---|
| 1 | "Create in org" at first link; within one flush window every item, PRD and comment is readable by a viewer; no excluded data in the payloads | **pass** |
| 2 | 500 status changes in 10 minutes stay at one flush per window and under the default budget | **pass (a test)** |
| 3 | Killing the box mid-backfill resends no confirmed batch | **pass (a test)** |
| 4 | `429` with `Retry-After: 30` delays the next send by at least 30 s | **pass (a test)** |
| 5 | A member's description edit is applied, attributed `cloud:<label>`, and visible in the replica after the next push; the same proposal against a stale version is `conflict` with the current value | **pass** |
| 6 | A viewer's proposal is refused by the console; a `done` proposal is refused by the box even though the console accepts it | **pass** |
| 7 | `read_only` makes the box refuse every proposal | **pass** |
| 8 | Unsync with purge removes the replica, and the console then answers 404 for the project's items | **pass** |

## 1. First link and the replica

Step ③ was set to **Sync to org** plus **Create in org**. Results:

- `POST /api/sync/projects` returned 201 and created cloud project `e2831870-…`.
- The contract check returned 200.
- The gzipped entity push returned 200.
- While WALK-13's decision was open, the box polled proposals about every 10 s, and an idle poll was a `304`.

On box 2026.11.48, backfill read `15 total, 15 confirmed, 0 parked, done`. The console replica held all 13 items, WALK-P1 and the wait, each at version 1, with freshness `live`.

**Viewer.** The walking account is a viewer in the demo org, and on the console before GBC-265 that made it a viewer for every write. That gave two results:

- Reads of the item list, item detail and PRD all succeeded.
- A proposal was refused `403 viewer_read_only`. That is the viewer half of clause 6.

**Exclusions.** The replica bodies of items, the PRD and the wait contain no email, API key, seat, embedding or transcript. Item body keys are:

`kind, tags, links, title, prd_id, status, blocker, line_id, evidence, acceptance, description, touchpoints, status_history`

The test half is `test_project_sync_payload.py`:

- `test_backfill_bytes_contain_none_of_the_seeded_secrets`
- `test_body_keys_equal_the_allow_list_and_a_model_field_does_not_join_it`

## 2–4. Budget, resume, back-pressure (tests)

The PRD scopes these clauses to tests:

- **500 changes:** `test_project_sync_flush.py::test_five_hundred_status_changes_over_ten_minutes_stay_inside_the_budget` and `test_project_sync_outbox.py::test_five_hundred_status_changes_collapse_to_one_row_version_500`.
- **Kill mid-backfill:** `test_project_sync_backfill.py::test_a_restart_sends_no_entity_the_manifest_already_confirmed`.
- **429:** `test_project_sync_flush.py::test_a_429_retry_after_sends_nothing_until_the_wait_is_over` and `test_project_sync_api.py::test_a_429_reports_held_by_retry_after_and_unknown`.

The live walk did see one of these behaviours: a box upgrade mid-walk restarted the box, and the backfill did not resend the 15 confirmed entities.

## 5–7. Proposals, on box 2026.11.53 and console rc.1

Each proposal below was posted from the console as the walking account, whose label is "Smoke Engineer". Each outcome was read back from `GET /api/projects/:id/replica/proposals`.

| Permission | Proposal | Outcome |
|---|---|---|
| `comments` | edit WALK-1 | `refused: comments allows only comment and answer` |
| `comments` | comment on WALK-2 | `applied` — box history `comment` by `cloud:Smoke Engineer`, proposal id 9600b098 |
| `proposals` | edit WALK-1 description, base v1 | `applied` — replica WALK-1 went to v2 with the new description; box history `update_item` by `cloud:Smoke Engineer`, proposal id 920f1936 |
| `proposals` | the same edit, base v1 again | `conflict`, with `current` = replica v2 and B2's description |
| `proposals` | edit WALK-3 `status: done` | console accepted it (201); box `refused: status done is never accepted from the cloud (v1)`; WALK-3 stayed `next` |
| `read_only` | comment on WALK-2 | `refused: read_only refuses every proposal` |
| `read_only` | answer WALK-13's decision | `refused: read_only refuses every proposal` |

Two earlier proposals were sent while GRPH-1842 was open: an edit and a comment. The box refused both, the edit for permission and the comment as `target not found`. On 2026.11.53 the box reported those stored outcomes, and the console showed them `refused` with the box's reasons. They were not applied again (exactly-once, D10).

## 8. Unsync with purge

`PUT /api/sync/projects/walk-sync {sync_level: "local", purge: true}` sent `DELETE /api/sync/project-entities?project=…`, which returned 200. The console then answered `404 not found` for:

- `replica/items`
- `replica/items/WALK-1`
- `replica/prds/WALK-P1`

The proposal history stays readable (200).

Found here: the box's status still reports the local project's backfill as `running 0/15` (GRPH-1850).
