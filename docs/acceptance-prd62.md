# PRD-62 capacity routing — D1–D10 conformance (GRPH-1246)

A read of S1–S5 as merged at `da43f5cc` (S1 GRPH-1247 #1229, S2 GRPH-1248 #1227, S3 GRPH-1249
#1230, S4 GRPH-1250 #1231, S5 GRPH-1251 #1232) against PRD-62 §5. Each decision gets a verdict:
**pass**, **partial** (the guarantee holds but a clause of the decision is missing), or **gap**.
Every gap is filed as its own item. Line numbers are at `da43f5cc`.

Tests run for this read: `backend` `test_entitlements.py`, `test_routing.py`,
`test_capacity_forecast.py` (56 passed); `fleet` `test_capacity_failover.py` (4 passed).

| | Decision | Verdict | Filed |
|---|---|---|---|
| D1 | Credentials never move; work does | pass | — |
| D2 | A subscription has no secret field | pass | — |
| D3 | API keys stay on the runner; the server holds a handle | pass | — |
| D4 | Five headroom states; unknown is not available | partial | GRPH-1256 |
| D5 | Reset text quoted, parsed only when proven | pass | — |
| D6 | The fallback ladder is policy, not code | **gap** | GRPH-1255 |
| D7 | Review reserve: anything not ruled out counts | partial | GRPH-1257 |
| D8 | Premium gate | pass | — |
| D9 | Naming | pass | — |
| D10 | Cross-runner routing is claim shaping, never proxying | pass | — |
| S1↔S3 | The fleet feeds the ledger | **gap** | GRPH-1258 |

## D1 — Credentials never move; work does: pass

- Server: no write path accepts a credential. `set_entitlement` refuses any unknown field and
  names D2 when the field looks like a secret (`backend/app/services/entitlements.py:68-82`). A
  subscription needs `owner_user_id`, the person signed in to it (`entitlements.py:150-152`).
- Runner declaration carries entitlement **ids** only, checked for existence in the org, and is
  labelled self-declared and unverified (`entitlements.py:240-258`, `DECLARATION_TRUST` at `:44`).
- Fleet (S1): failover only moves between adapters installed on the same machine, resolved
  through the operator's own matrix (`fleet/src/gbfleet/until.py:573-596`, `:598-635`). Nothing in
  `until.py` or `supervisor.py` sends a login anywhere. The child's Graphban credential is its
  own seat file, outside the worktree (`fleet/src/gbfleet/adapters/claude.py` `seat_path`), and
  `mcpshare.RESERVED` keeps the operator's Graphban credential from being copied into a child
  (`fleet/src/gbfleet/mcpshare.py:37-40`). Vendor sign-ins stay where the vendor CLI wrote them.

## D2 — A subscription entitlement has no secret field: pass

- Structural: `CHECK (secret_ref IS NULL OR kind IN ('api_key', 'cloud'))`, named
  `ck_entitlements_secret_only_api_key_cloud`, in the migration
  (`backend/alembic/versions/0162_entitlements.py:37-38`) and the model
  (`backend/app/models/__init__.py:3379-3382`).
- API: a `secret_ref` on a non-secret kind is refused after the write is applied, so a kind
  change cannot leave one behind (`entitlements.py:144-149`). Credential-shaped field names are
  refused rather than dropped (`entitlements.py:72-80`).
- `to_dict` renders `secret_ref: null` on a subscription rather than omitting it
  (`entitlements.py:226-228`).

## D3 — API keys stay on the runner; the server holds a handle: pass

- `secret_ref` must be a handle (`_HANDLE`, `entitlements.py:54`); a value with a known raw-key
  prefix (`sk-`, `AKIA`, `ghp_`, ...) is refused as "the key itself" (`entitlements.py:57-58`,
  `:94-106`).
- The server holds exactly the handle, an owner (`owner_user_id`) and a budget (`token_budget`,
  NULL = not stated) (`models/__init__.py:3364-3370`).
- Only planners may write the registry (`backend/app/services/fleet.py:1290-1293`).

## D4 — Headroom states, and unknown is not available: partial

Holds:

- The state is derived from the append-only `entitlement_events` ledger on read and never
  stored (`backend/app/services/headroom.py:75-94`). No events = `unknown` (`headroom.py:80`).
- `unreachable` is derived from presence of every declaring runner and overrides the stored
  state without erasing it, so the old state returns with the runner (`headroom.py:61-72`, `:81-82`).
- `broken` (`spawn_failed`) is its own state, separate from `exhausted` (`limit_hit`)
  (`headroom.py:34`). Both admit one probe child; nothing bursts onto `unknown`
  (`headroom.py:121-129`).
- Routing ranks `available` above `unknown` and skips the other three
  (`backend/app/services/routing.py:41`, `:48-61`).

Missing (GRPH-1256):

- "The UI names the runner that made the claim" for `broken`: the capacity payload has no
  declaring runner (`headroom.py:191-205`), and `CapacityPanel` shows only vendor, plan, state and
  the vendor text (`web/src/features/harness/CapacityPanel.tsx:38-52`). `list_entitlements` has
  `declared_by` (`entitlements.py:233`), but the Observe view does not read it.
- "Until its runner re-declares it": re-registering with the entitlement records no event, so a
  `broken` entitlement leaves `broken` only through a probe child that registers.

## D5 — Reset times are quoted, then parsed only when proven: pass

- The adapter keeps the vendor's line verbatim (`fleet/src/gbfleet/adapters/claude.py:41-58`),
  and S1 carries it as `said` into the wave's `exhausted` map and the terminal JSON
  (`until.py:467-472`; `Report.payload` "exhausted").
- The ledger stores `vendor_text` verbatim and requires it for `limit_hit` / `spawn_failed`
  (`headroom.py:118-119`, `:130-132`). No code parses it into a time (`headroom.py:87`,
  `:199-201`). `next_reset_at` is stated by the owner, never parsed
  (`models/__init__.py:3371-3373`).
- `exhausted` lifts only when the holder reports `registered` (`headroom.py:15-19`). No measured
  shape is parsed yet. That is the conservative end of D5: a parse is allowed once a shape is
  proven, and none is claimed.

## D6 — The fallback ladder is policy, not code: gap (GRPH-1255)

- The order is hard-coded in `routing.rank`: `available` before `unknown`, then soonest stated
  reset (`routing.py:59-60`). It does not read `kind` or `token_budget`, so the PRD default
  "subscription with headroom → org API key within budget → local gbagent" is not expressed.
  An API key ranks the same as a subscription, and an over-budget key is not skipped.
- When nothing has headroom, `gate` allows the claim as "next rung of the ladder"
  (`routing.py:144-145`). No rung is named or configurable.
- `projects.fleet_policy` (`POLICY_KEYS`, `backend/app/services/fleet_profiles.py:20-21`) and the
  user profile have no ladder key. An org cannot remove the API-key rung.

## D7 — Review reserve: anything not ruled out counts: partial (GRPH-1257)

Holds:

- Counted per vendor over `available` and `unknown` only: `review_reserved` asks whether any
  *other* vendor is in `rank`, which drops `exhausted`, `broken` and `unreachable`
  (`routing.py:89-91`). A second entitlement on the builder's own vendor does not count.
- Enforced on claim (`routing.py:114-124`) and shown on proposals (`routing.py:229-250`). The
  "three unknown on A plus one unknown key on B" example builds on A
  (`backend/tests/test_routing.py`).

Missing:

- "The Observe page warns when the reserve rests only on `unknown` entitlements": no such
  warning exists in the capacity payload, R7, or `CapacityPanel`.
- "Reviewer-eligible": every other vendor counts, including a `local` entitlement nobody has
  enabled for review. Nothing filters by reviewer eligibility.

## D8 — Premium gate: pass

- `Plan.capacity_routing` is true only on `team` and `enterprise`
  (`backend/app/services/quotas.py:50-64`). `require_capacity_routing` refuses with 402, and a
  project with no org (self-host) is refused rather than let through (`quotas.py:147-164`).
- Gated: registry read and write (`entitlements.py:164`, `:183`, `:250`), the ledger
  (`headroom.py:101`), the capacity view (`backend/app/routers/harness.py:57-65`,
  `headroom.py:246`), R7 (`backend/app/services/harness_rules.py:839-840`), and routing,
  which turns itself off off-plan (`routing.py:104-105`, `:234-235`).
- S1 has no plan check: it lives in the open-source fleet (`until.py:598-635`).

## D9 — Naming: pass

- The feature is "capacity routing" in code, errors and docs (`quotas.py:161`). The Observe
  panel is titled "Capacity" (`CapacityPanel.tsx:17`). Vendor names appear only as data rows
  (`CapacityPanel.tsx:40`), not in chrome. The harness placeholders in `Preferences.tsx` belong
  to PRD-37, not to this feature.

## D10 — Cross-runner routing is claim shaping, never proxying: pass

- `routing.gate` only decides whether an item is **offered** to a claimant (`routing.py:94-145`).
  `claim_next` skips a held item (`backend/app/services/items.py:2439-2440`), and `claim_item`
  returns None for one (`items.py:2608-2609`). `claim_cluster` claims through `claim_item`, so
  it is gated too.
- The soft reservation is written only when exactly one buildable entitlement exists, with
  `RESERVATION_TTL = 10 min` (`routing.py:40`, `:129-143`). It lapses on the clock
  (`routing.py:157-163`) and ends at once on `limit_hit` / `spawn_failed`
  (`headroom.py:133-137`, `routing.py:189-197`).
- The attempt record logs the claimant's declared vendor and model, and `matched` against the
  reservation (`routing.py:171-186`). Nothing executes or proxies on another runner's behalf.

## S1↔S3 — the fleet does not feed the ledger: gap (GRPH-1258)

S1's failover keeps its exhausted marks in the wave's memory (`until.py:467-472`). gbfleet never
declares `entitlements` at `register_agent` and never calls `report_entitlement_event`: neither
appears anywhere in `fleet/src`. So in a gbfleet wave, every server-side entitlement stays
`unknown`, a limit hit never ends a reservation, and S4 routing and S5 forecasting have no
producer. Each server slice is correct against its tests. Wired together, the server ledger
stays empty.
