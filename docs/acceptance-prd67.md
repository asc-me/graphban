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
