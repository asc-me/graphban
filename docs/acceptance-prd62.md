# PRD-62 acceptance walk — capacity routing (GRPH-1252)

Walked at `da43f5cc` against the test suites, on SQLite. Postgres was not run.

**Not walked: real vendor limits.** Criterion 1 asks for a wave to hit a *real* vendor
limit. That needs a live, exhausted vendor account and a running wave, which a worktree
build cannot do. Criterion 1 is therefore covered by the stubbed-limit failover tests only,
and a person still needs to do the real-limit run. The vendor-text strings are the measured
ones the adapters already carry, so the stub is faithful to the text, not to the vendor.

| # | Criterion | Evidence | Result |
|---|-----------|----------|--------|
| 1 | Limit on first adapter → remaining items finish on another; terminal JSON quotes the vendor | `fleet/tests/test_capacity_failover.py` (continues on next candidate, never returns; `exhausted` always in terminal JSON) | pass on stub; real limit **not walked** |
| 2 | Every candidate exhausted → reason `capacity`, not `cap` | `test_no_candidate_left_ends_the_wave_capacity_and_quotes_the_reset` | pass |
| 3 | `subscription` + secret refused at API, impossible in schema | `backend/tests/test_entitlements.py` (REST, MCP, CHECK constraint, kind-change) | pass |
| 4 | No ledger events → `unknown`, never `available` | `test_an_unreported_entitlement_is_unknown_not_available`; `web/src/__tests__/capacity-panel.test.tsx` (renders unknown) | pass |
| 5 | Two available → sooner reset chosen, attempt record says so | `backend/tests/test_routing.py` (`"soonest reset" in rec["reason"]`, and not claimed when no reset decided) | pass |
| 6 | Build leaving no reviewer-eligible vendor is not scheduled | `test_a_build_that_would_leave_no_reviewer_vendor_is_not_scheduled` | pass |
| 7 | S2–S5 refused on `free`/`pro` with 402 / `quota_exceeded` | `test_entitlements.py` (REST + MCP), `test_capacity_forecast.py::test_capacity_is_premium` | pass |

Runs: backend `test_entitlements`, `test_routing`, `test_capacity_forecast` 56 passed;
fleet `test_capacity_failover`, `test_headroom` 18 passed; web suite 1662 passed.
