# PRD-67 guided setup — §10 gaps G1–G13 conformance (GRPH-1437)

A read of `main` at `d4822ccb` against GRPH-P67 §10. "Folded into the text" is not "shipped",
so each gap gets a verdict: **shipped**, **partly** or **missing**, with the file or test that
proves it. Every partly/missing gap is filed as its own item and linked to GRPH-1437.

## §10 conformance

| # | Resolution | Verdict | Proof | Filed |
|---|---|---|---|---|
| G1 / D4 | Request an org on the self-host closing tour card | shipped | `web/src/features/tour/OrgPlug.tsx:67-68`; `backend/app/routers/auth.py:272`, `services/setup.py:275`; tests `web/src/__tests__/org-plug.test.tsx` ("Request an org sends name, email and note…", "Request again appears only once the stored date is 14 days old") | — |
| G2 / D5 | ③ CLI route says "if you haven't already, run `gban setup`", stays *pending* | shipped | `web/src/features/onboarding/ConnectStep.tsx:237`; test `QuickStartDialog.test.tsx` "the gban route shows three commands, Next is enabled, and ③ stays PENDING through First job". Machine reports (S7) exist in `backend/app/services/machines.py` | — |
| G3 | Compose log prints the URL plus "open this to continue setup" | **missing** | no such line in `docker-compose.yml`, `start.sh`, `backend/app/main.py`, `bootstrap.py` (grep for "continue setup" finds nothing); `docs/getting-started.md:18` lists the URL only | GRPH-1443 |
| G4 / D6 | Triggers on onboarding state; ① confirms an existing project | shipped | trigger `web/src/components/shell/AppFrame.tsx:114-117`; confirm-vs-create `QuickStartDialog.tsx:649` (`creating = !projectId`), `:685`; tests "who lands in the dialog" and "the chip reopens on the first unfinished step…" (existing project) | — |
| G5 / D6 | Existing users backfilled as finished, plus a one-time "Take the tour" toast | **partly** | backfill shipped: `backend/alembic/versions/0186_setup_onboarding.py:30-35`, test `test_setup_project.py::test_migration_backfills_existing_users_as_finished`. The one-time toast does not exist (no toast in `AppFrame.tsx`; only Help/palette replay at `:133`) | GRPH-1444 |
| G6 | Mode-specific ④ prompt (hosted asks for the dry-run estimate first) | shipped, untested | `QuickStartDialog.tsx:1022-1026` `firstJobPrompt(hosted)`; no test asserts either prompt text | — (noted) |
| G7 | BYOK plaintext warning inline on the Claude card when `SECRET_ENCRYPTION_KEY` is unset | **partly** | inline beside the paste box (`QuickStartDialog.tsx:~990`) but shown unconditionally as "If … is unset", though `secret_encryption_key_set` is already in the posture payload | GRPH-1445 |
| G8 / D7 | Step ⓪; ③ commands use the entered address | shipped | `QuickStartDialog.tsx:571,532`; `ConnectStep.tsx:140,232,257`; tests "step ⓪ on a dedicated server — stores the address…", `ConnectStep.test.tsx` "adds hints (curl on a dedicated server)" | — |
| G9 | Picker reads one CLI-exported list | shipped | `cli/src/gban/setup.py:54` `SETUP_AGENTS` → `scripts/gen_gban_agents.py` → `web/src/lib/gban-agents.json` → `ConnectStep.tsx:33`; tests `cli/tests/test_setup.py:1282` (generated file in sync), `ConnectStep.test.tsx` "the gban route list is the CLI's export" | — |
| G10 | Empty-state copy per stop; no faked data | shipped | `web/src/features/tour/stops.ts:18-62`; test `app-tour.test.tsx` "shows empty-state copy on an empty view, and none when the count is unknown" | — |
| G11 | Self-host ④ "second agent for reviews" line | shipped, untested | `QuickStartDialog.tsx:1090`; no test asserts it | — (noted) |
| G12 | `docs/getting-started.md` and `content.ts` updated | shipped | `docs/getting-started.md:26,48,69-71`; `web/src/features/docs/content.ts:36-37` | — |
| G13 | `last_used`: first write immediate, later throttled 1/min/key, outside the request transaction | shipped | `backend/app/security/apikey.py:117-141` (own `Session`, `LAST_USED_THROTTLE_SECONDS`); test `test_connect_step.py::test_first_call_stamps_immediately_later_calls_are_throttled`. (The column is `last_used`, not `last_used_at`.) | — |

## Sabotage

Run locally (`backend`, `uv run pytest`), each reverted afterwards:

| Sabotage | Result |
|---|---|
| G5: migration 0186 `upgrade()` no longer calls `backfill()` | `test_setup_project.py::test_migration_backfills_existing_users_as_finished` **fails** |
| G13: the first write is throttled/skipped (`if first: return`) | 3 fail: `test_pending_until_the_first_call_then_done_with_evidence`, `test_first_call_stamps_immediately_later_calls_are_throttled`, `test_revoked_key_with_a_call_does_not_tick` |
| G13: the throttle removed (`if False:`) | `test_first_call_stamps_immediately_later_calls_are_throttled` **fails** |

A first attempt at G13 (only making the first write pass through the throttle check) left all
6 tests green. That change does not alter behaviour, because a first write has no cached
entry, so it was not a real sabotage.

## Findings

- G3, G5 (toast) and G7 (conditional) are real gaps; each is its own item.
- G6 and G11 ship but nothing would fail if the copy changed; worth a test when someone next
  touches `QuickStartDialog.tsx`. Not filed separately.

# §8.1–8.2 fresh-install walk and sabotages (GRPH-1434)

Walked 2026-11-12 by a planner session against `main` at `2f0d6ea4` (product version 2026.11.17).

- **Box:** a scratch compose stack, `docker compose -p gb-walk-1434 down -v && up --build`, with its own project name and ports (API `:8491`, web `:8492`). `docker-compose.yml` pins `name: agentledger`, so a bare `down -v` from any checkout targets the live project; always pass `-p`.
- **Agent:** a real Claude Code.
- **CLI:** `graphban-cli` 0.8.8 and `graphban-fleet` 0.18.20, installed from PyPI under a clean `$HOME`.

## §8.1 the walk, step by step

| # | Step | What was shown | Verdict |
|---|---|---|---|
| 1 | `up --build` | API log: four `JWT_SECRET is weak or the built-in default` warnings (one per worker), then `Graphban is ready at http://localhost:8492 - open this to continue setup` | G3 shipped |
| 2 | First load, zero users | ~3 s skeleton, then **"Sign in · Welcome back"**; no first-run owner prompt | GRPH-1552 (1) |
| 3 | Create account | An `@example.test` email is refused (reserved TLD, correct), but the error is the raw 422 JSON body | **GRPH-1551** |
| 4 | Welcome → Get started (zero projects) | Dialog closes; shell is header only, no rail, empty `<main>`; survives reload. Only the "Finish setup" chip reopens it (on Sections, correctly) | **GRPH-1542** |
| 5 | ⓪ Where | "On this computer" pre-selected ("you opened Graphban on localhost"). ⓪ shows DONE once ① exists | ok |
| 6 | ① Project | Name, confirmation tick, repo URL under Advanced. Posture box: "4 settings to fix", with JWT_SECRET placeholder, `SIGNUP_MODE`, `SECRET_ENCRYPTION_KEY`, each with a copyable fix. Rail and dashboard appear only now | ok |
| 7 | ② Model | Offline (stub) default → DONE | ok |
| 8 | ③ gban route | `uv tool install …` assumes `uv` (GRPH-1552 (2)). `gban login --pair` → code → Approve → "Approved — your terminal is signed in as you"; session stored 0600. `gban setup` in `~/code/walk-app` matched the project by git remote and wrote 78 pack files, then reported **2 UNKNOWN**: `graphban-answer/SKILL.md … differs from the one shipped here` (the PyPI CLI bundles the pre-GRPH-1524 copy). The report is never clean, so **③ never went ✓ on this route**. The dialog said only "Pending — no first call seen yet." | **GRPH-1543**, **GRPH-1544** |
| 9 | ③ paste-a-key route | "Create a key for Claude Code" → command + `.mcp.json`. Real Claude Code, `claude -p "what's in my backlog?"` → "Your backlog is empty … Walk Project (WP)". Within ~5 s: "✓ Connected — First call from key 'quick start · Claude Code' at 2026-11-12T13:06:41.891806+00:00" | **✓ by itself** |
| 10 | ④ First job | The dialog's prompt, pasted into real Claude Code: 2 memory candidates (from AGENTS.md) and 3 items. Code graph not built: "needs `describe_code`, which my key doesn't have". ④ stays pending, saying "Your agent is working" after the agent exited | **GRPH-1545** |
| 11 | Finish → tour | "You're set up" (first job pending; "Finishing doesn't mark them done"). Tour: Dashboard, Tracker, Specs, Agent sidebar, Memory review, Code graph, Fleet, Activity, then the Organization end card (Request an org / Not now / Link to an org) | ok |
| 12 | Back into setup | Chip gone. Help lists only Concepts / Take the tour / Start-up prompts, though Welcome promises a way back from Help. The only route is the dashboard card's "Verify with a key instead" | **GRPH-1546** |

Clean-`$HOME` Claude Code is "Not logged in · Please run /login" (interactive), so the walk drove the real Claude Code with `--mcp-config <walk config> --strict-mcp-config`.

## §8.2 sabotages

| Sabotage | Observed | Flips? |
|---|---|---|
| Revoke the key | Card drops "Agent connection verified" and falls back to the machine-report state; reopened dialog shows ③ PENDING; the revoked key on `/api/mcp` → 401 | **yes** |
| Rename a nav item | Hosted `tour="org"` (LeftNav.tsx ~433): 4 hosted variants fail. Self-host loading-state anchor (~794): 4 self-host variants fail. Self-host **loaded** `OrgRailItem` button `data-tour="org"` (~801): 18/18 pass | **partly**: GRPH-1549 |
| Stop the API mid-poll | Fresh key, first call 200, ③ ✓ in the open dialog; `docker stop` the API. After ~20–40 s every step reads "? UNKNOWN", not a stale ✓. But each reason is the raw nginx 502 page, and the dashboard Setup status card and MCP chip kept "✓ Agent connection verified" / "78 tools live" | dialog **yes**; card no: GRPH-1547 |
| Placeholder `JWT_SECRET` | `/api/setup/instance` `jwt_secret_set`: false (placeholder) → true (`JWT_SECRET=$(openssl rand -hex 32)`) → false (put back). The UI hint is shown at baseline | **yes** |
| Machine report with a FAIL line | `gban doctor` with graphban removed from `~/.claude.json` reports no FAIL (GRPH-1550), so the report was posted directly to `/api/machines/report` for the same machine. With the quick-start key live: ③ stays ✓ (proof `first_call`). With the key revoked: ③ pending, "vicious-apogee reported 1 FAIL", and the card shows the FAIL line in red | key live **no**: GRPH-1548; key revoked yes |

## Verdict

**Not accepted yet.**
- ③ went ✓ by itself on the paste-a-key route, but not on the `gban setup` route (GRPH-1543).
- Two sabotages flip only partly (GRPH-1548, GRPH-1549).
- Re-walk §8.1 step 8 and those two sabotages once the linked fixes land.

Findings filed and linked to GRPH-1434: GRPH-1542, 1543 (blocks), 1544, 1545, 1546, 1547, 1548, 1549, 1550, 1551, 1552 (seven small issues).

# §8.4 link walk (GRPH-1523), first attempt

Walked 2026-11-12 against a scratch console built from graphban-control `main` (`46030a6`), on `:8493` with its own Postgres, and the GRPH-1434 scratch box. Production was never contacted: the box's Console URL field is pre-filled with the production console, and it was changed to the scratch one.

| Step | Observed | Verdict |
|---|---|---|
| Admin grants a seat and one of two projects | Console: org "Walk Org" with Cloud Alpha and Cloud Beta; engineer and second member joined by org invite (member = seat); team Eng holds write on **Cloud Alpha only** | ok |
| Engineer signs in with the device code | Box Settings → Cloud / Sync: "Get a sign-in code" → `SK2P-6Y9E`; console `/link` as the engineer → Look up (box label, org, "can push only to projects you can reach") → Approve → box shows **LINKED**, "Signed in to Walk Org as walk-eng@example.com" | **pass** |
| Step ③ shows only the granted project | Each of the three local projects offers "Local only" and **Cloud Alpha**; Cloud Beta is absent | **pass** |
| Sync one, leave two local | Walk Project → Cloud Alpha; Local Two and Local Three local only; `/api/sync/status` agrees | **pass** |
| First push | Empty graph: "Pushed 0 nodes and 0 edges to CLOUD ALPHA" (ok), but **no request reached the console** (GRPH-1553). With 2 nodes (`describe_code`): `POST /api/sync/code-graph` → **404**; the console has no such route, although `docs/contract.md` lists it (GBC-82). Console deployment stays `freshness: never` | **fail** |
| Sabotages (grant removed, seat removed, takeover, console down, expiry, migrated key) | Not run: each push-based check would show the same 404, not the behaviour under test | blocked on GBC-82 |

Also found: `docker-compose.yml` doesn't forward `SYNC_CLOUD_URL`/`SYNC_API_KEY` (GRPH-1554). `describe_code` is callable with an agent key but missing from its `tools/list` (noted on GRPH-1545). The console `/link` page renders browser-default controls, and its expiry is a raw ISO timestamp (polish).

**Verdict: blocked.** Re-walk from the first push once GBC-82 decides where a linked box's code graph goes.

# §8.4 link walk (GRPH-1523), re-walk after GBC-82

Walked 2026-11-12 16:29–16:42Z, after GBC-82 merged (graphban-control `b483a85`, `POST /api/sync/code-graph`). The scratch console was rebuilt from the GBC-82 commit, which has the same tree as `b483a85`, keeping its database. The box was the same scratch box (graphban `2f0d6ea4`).

**Setup used throughout.** GRPH-1553 is still open: a push with nothing changed never contacts the console. So before each push below, the box's `code_sync_state` manifest hash for `app/greet.py` was set to a stale value, which makes a real request reach the console. For the expiry check, the code's `expires_at` was moved into the past in the scratch console database instead of waiting ten minutes. For the migrated-key checks, the box was linked with a console sync credential and then the migration step `0194.migrate_key_links` was run on it, which is how a pre-upgrade box arrives.

| Step | Observed | Verdict |
|---|---|---|
| First push | Box: `done`, 2 nodes, 1 edge; sync status `ok`. Console: both nodes stored (`stale=false`) plus the edge; `box_deployment_projects` has the project; `last_pushed_at` set, so freshness left `never` | **pass** |
| Grant removed | Team grant on Cloud Alpha deleted → push `refused no_project_access`, Walk Project red; link stays `ok`; Local Two/Three unchanged. Grant restored → next push `done` | **pass** |
| Seat removed | Engineer removed from the org → push `refused seat_removed`; box shows **LINKED** with "Your seat in Walk Org was removed. Nothing is syncing." and Sign in again | **pass** |
| Takeover | Second member (seat, team Eng) approves a new device code → new deployment, box linked as `walk-m2`, mapping Walk Project → Cloud Alpha kept; next push checked again and `done` | **pass** |
| Console stopped mid-poll | Sign-in started, console stopped → poll `unknown` ("could not reach … ConnectError"), not refused; the stored link row is byte-identical | **pass** |
| Code expired | Approve → `410 expired`; box poll `refused: expired`; link unchanged. Minor: `sign_in_pending` stays true (GRPH-1557) | **pass** |
| Migrated key link | Keeps pushing (`done`; the console records the key's `last_pushed_at`); muted "This deployment still uses an old link key…" warning. Sign in → the console revokes the key (a push with it is 401), `legacy` clears, the warning goes. **But** the pinned mapping keeps an empty cloud project, so the next push is 422 "X-Cloud-Project is required with a deployment token." and Walk Project shows REFUSED with no target (GRPH-1556) | **fail** |
| Migrated key refused in the console | Key revoked in the console → push `refused key_refused`; box shows red "This link used a key. Sign in to keep syncing." with Sign in again | **pass** |

Also found: the box's purge sends `DELETE /api/sync/code-graph`, which the console does not serve (GBC-83). The box sends all edges in one request, and the console refuses more than 20000 (GRPH-1555).

**Verdict: blocked on GRPH-1556.** Every other step passes. Re-walk the migrated-key sign-in once GRPH-1556 lands.

## Migrated-key sign-in, confirmed on main

Two fixes were needed. GRPH-1556 (asc-me/graphban#1616, merged `c21e446d`) makes the box fill the pinned project after sign-in. GBC-84 (asc-me/graphban-control#95, merged `b6a3811`) makes the console's poll answer name the revoked key's project. The walk was first run against both PR heads (17:38Z), then again on main at 18:14Z: box API at `c21e446d`, console at `b6a3811`.

| Step | Observed | Verdict |
|---|---|---|
| Migrated key link | Fresh console sync credential on Cloud Alpha → box key link + `0194.migrate_key_links` → `legacy=True`; push `done` | **pass** |
| Sign in | Second member approves → console revokes the key; box `legacy=False`, Walk Project's cloud project filled with Cloud Alpha | **pass** |
| Next push | `done` to Cloud Alpha, row IN SYNC, relink warning gone | **pass** |
| Old key | A direct push with it gets 401 | **pass** |

**Verdict: pass.** Every §8.4 step has now passed: grant, sign-in, step ③, first push, and all seven sabotages. Two items are still open but don't block this walk: GRPH-1553 (an unchanged push never contacts the console; it was worked around here) and GRPH-1557 (`sign_in_pending` stays set after an expired code).
