# PRD-38 — Harness telemetry: attempt records, the Harness page under Observe, recommendations as drafts, org and platform views

**Ledger id:** GRPH-P38
**Status:** draft — v0.1, not yet grilled.
**Depends on:** PRD-37 (matrix, profiles, policy, measured cells, the explained resolution) · PRD-35 (the delegation record: requested tier, declared vendor/model, outcome stored at the event) · PRD-36 (bound seats, `spawn(tier)`) · PRD-16 (lessons as `MemoryShard` candidates → review → published) · PRD-33/34 (Observe: Live board and feed) · PRD-1 (orgs, hosted mode, the operator console)
**Complemented by:** `docs/design-observe-lessons.md` (the Lessons page: effectiveness as a computed judgement) · `gbfleet doctor` (prints the matrix against the machine, under the server's profile) · GRPH-732 (children declare vendor/model/tier, so cells attribute)
**Touches:** `backend/app/models/__init__.py` (`AttemptTelemetry`, `HarnessRollup`, `RecommendationDismissal` new; `Organization.telemetry_share`) · `backend/alembic/versions/0111_*` · `backend/app/services/delegation.py` (`measured` gains a window and a difficulty band) · `backend/app/services/harness.py` (new: derivation, rollups, recommendations, lesson drafts) · `backend/app/routers/harness.py` (new: the page's reads, the supervisor's attempt write, org and platform aggregates) · `backend/app/services/learning.py` (candidate from a recommendation) · `backend/app/mcp_server.py` (result payloads only: `brief.measured_for_lane`) · `fleet/src/gbfleet/{spawn,supervisor,mcp,until,client,doctor}.py` (the exit report) · `web/src/features/harness/` (new) · `web/src/features/orgadmin/` · `docs/mcp.md` · `docs/data-model.md` · `docs/api-reference.md`

---

## 1. Overview

PRD-37 gave the supervisor a way to choose a harness and model for a tier and to explain the choice. It also gave the ledger its first measurement of how those choices turn out: one cell per vendor × model × lane × requested tier, holding signed-off-over-finished and a median time, with `n`. That is telemetry. It is also thin: one outcome bit per attempt, no record of what kind of work the attempt was, no cost, no version of the binary that ran, no reason for a bounce, and no place where a human can look at the trend and decide anything.

This PRD makes the platform accumulate that knowledge over time and put it in front of the people who act on it, without letting it act on its own. Three parts:

1. **The attempt record.** Every delegation that finishes gets a telemetry row: what the work was (task class, lane, a size band), who ran it (vendor, model, the harness binary's version, the requested and declared tier), how it went (outcome, bounce category, turns, wall clock, tokens where the harness reports them), and how it was chosen (the resolver's first choice or a fallback). The server derives what it can from the ledger at outcome time; the supervisor posts the runtime facts it alone has, at child exit, over a narrow API-key REST route. No MCP tool, no schema property.
2. **The Harness page under Observe.** Per project: each cell's rate over time, with `n`, its version, its sampling skew, and its cost proxy, at the difficulty band the work actually had. Beside the charts, **recommended changes** — cards a rule produced from the cells, each carrying the evidence and a replay of what the resolver would have done differently. Accepting one is a human act through a surface that already exists: a commit to the matrix, a PUT to a profile or a policy. Nothing is applied by the page.
3. **The same view for an org, with a platform overlay.** An org admin sees the cells across the org's projects. On the hosted service, an org that opts in contributes anonymised cell aggregates, and every contributing org can overlay the platform average on its own charts once enough orgs contribute for the average to say nothing about any one of them.

Knowledge that reaches a chart but not an agent is half-delivered, so a recommendation that a human accepts, and a cell that crosses the sample floor, each draft a **lesson candidate** into the PRD-16 review inbox. Published, it reaches agents the way every lesson does.

### 1.1 What this is not

- Not a chooser. The resolver still resolves by PRD-37 D5 under the profile and policy; this PRD adds measured cells it already reads (with a window and a band) and nothing else to the resolution path.
- Not a job that edits the matrix. A row moves to `verified` or `failed` by a commit that names the item (PRD-37 non-goal, kept). The page drafts the commit's evidence; a person makes it.
- Not a bandit. An exploration budget — deliberately sending every Nth cheap-eligible item to the runner-up so cells fill without bias — costs real money and belongs in its own PRD. This one shows the skew so a person can choose to explore by hand.
- Not billing. Tokens are recorded when the harness reports them; cost stays a class. The page shows tokens per signed-off item as a proxy and says so.

## 2. Problem

### 2.1 One bit per attempt

`measured` knows that an attempt ended `signed_off` or not. It does not know that the item was a two-line doc fix or a schema migration, that it was the third attempt after two bounces, that the child ran 38 turns against a 40-turn budget, or that the bounce said "did not run the tests". A rate over those is a rate over noise, and it cannot be compared across cells.

### 2.2 The samples are the operator's habits

PRD-35 named the bias: frontier only sees what cheap failed. PRD-37 sharpened it: the harness the profile prefers gets the samples. A learning system fed by those samples learns the preference back and calls it a measurement. The rate must carry how the attempt came to be sampled — first choice, fallback, escalation — and the page must show the skew rather than hide it inside an average.

### 2.3 Difficulty is not recorded

A harness that is handed doc items looks better than one handed backend refactors. Until the record carries a proxy for difficulty, the rates say who got the easy work.

### 2.4 Sparsity, and versions that move

The gbagent backend/cheap cell reached `n=5` after two months on one project. Split by binary version and model name it is thinner still. `claude-fable-5-1` today; an alias tomorrow. Without the version on the record, a cell pools a harness with its own past; with it, cells rarely reach the floor. Both must be visible: current version by default, history on request.

### 2.5 Nothing feeds lessons

PRD-16 built the path from what happened to what an agent should know next time. Harness fitness — "for frontend items this quarter, cheap bounced three of four" — is exactly the kind of lesson it was built for, and nothing produces one.

### 2.6 There is no org view, and no baseline

An org running five projects has five thin sets of cells and no way to see them together. A single-project org has no way to know whether its 0.6 signed-off rate on cheap is normal. The only baseline anyone has is the matrix's committed evidence, which is one repository's history.

### 2.7 The manifest has eight tokens of headroom, and REST is session-only

The supervisor holds facts the ledger cannot derive — the binary version, turns and wall clock, tokens — and has no way to write them: every MCP tool addition is refused by the ceiling, and the REST surface authenticates humans, not keys. The plumbing decision is part of this PRD.

## 3. Goals

1. Every finished delegation leaves a telemetry row with the work, the runner, the outcome and the sampling reason, derived by the server where it can and posted by the supervisor where only it knows.
2. A Harness page under Observe shows each cell's rate over time with `n`, version, skew, difficulty band and cost proxy, per project and per org.
3. Recommended changes appear as cards with evidence and a replay, and are applied only by a human through an existing surface.
4. Accepted recommendations and cells crossing the floor draft lesson candidates into the PRD-16 inbox.
5. On the hosted service, opted-in orgs contribute anonymised aggregates and see the platform average overlaid, under a k-anonymity floor.
6. The resolver's measured cells gain a recency window and a difficulty band, and remain explained.
7. No new MCP tool and no schema property.

## 4. Non-Goals

- Changing matrix status, profiles or policy automatically.
- An exploration budget or any bandit (a later PRD).
- Pooling across lanes, tiers, task classes or versions in any number a recommendation rests on. Pooling is allowed only in display, labelled as such.
- Cost accounting in currency.
- Sharing anything but cell aggregates across orgs; raw attempt rows never leave their org.
- A learned scoring model. Every number shown is a count, a rate or a median, and every recommendation names the rule that produced it.

## 5. Key decisions

| # | Decision | Detail |
|---|---|---|
| D1 | The unit is the ATTEMPT, one row per finished delegation | `attempt_telemetry` keyed by `delegation_id` (unique). Written once, at the outcome event, from the ledger; enriched once, at child exit, by the supervisor. Open, expired and closed-without-outcome delegations get no row: an attempt that never ran teaches nothing about the harness. |
| D2 | The server derives what the ledger already knows | Outcome, bounce category (from the bounce reason's first clause, mapped to a closed set: `tests`, `scope`, `quality`, `process`, `other`), attempt number (prior finished attempts on the item + 1), lane, requested and declared tier, vendor and model (from the child's declared capabilities, GRPH-732), task class (the brief's), size band (`S` ≤2 touchpoints and ≤600 description chars, `L` ≥6 touchpoints or ≥2400 chars, else `M`), claim-to-finish seconds, and **how it was sampled**: `first_choice` when the child's declared vendor/model equals the resolution's winner recorded on the seat, `fallback` when it equals a dropped-as-not-installed row's runner-up, `explicit` when the spawn named the adapter, `unknown` otherwise. |
| D3 | The supervisor posts what only it knows, at child exit, over REST with the API key | `POST /api/fleet/attempts` accepts `{delegation_id or enrolment_code, binary_version, turns_used, turn_budget, wall_seconds, tokens_in, tokens_out, exit_meaning}` authenticated by `X-API-Key` (the first API-key REST route; scoped to keys that carry `fleet_status`). Idempotent on the delegation. The MCP manifest is untouched. Tokens come from what the harness prints: qwen's `-o json` result record, gbagent's spans, claude's `--output-format json` result; vendors that print nothing leave the fields null, and null is shown as "not reported", never as zero. |
| D4 | Rates are shown at the difficulty band the work had | Every cell is vendor × model × lane × tier × task class × size band. The page collapses task class and band by default with the pooled label visible; a recommendation never pools across them. |
| D5 | Versions are on the record and default to "current" | `binary_version` and `model` are cell keys. The page shows the current version's cells and offers history; a recommendation cites the version it is about. |
| D6 | The sample floor and the skew travel with every number | `n ≥ 5` (PRD-37 `MIN_SAMPLE`) for any rate to count; below it the cell is drawn grey with `n`. Every rate shows `first_choice / fallback / explicit / unknown` counts. A rate whose samples are more than 80% one sampling reason carries a "sampled by preference" badge. |
| D7 | Recommendations are drafts produced by named rules, and only a human applies them | Rules: **R1 promote** — an `unverified` row with ≥10 finished attempts in the window, signed-off ≥0.8 at any band, drafts matrix evidence text (item ids, dates) for a `verified` commit. **R2 demote** — a `verified` row with ≥6 finished and signed-off ≤0.25 drafts a `failed` entry naming the items. **R3 reweight** — a user's profile whose top default is beaten on signed-off by ≥0.3 at `n≥8` in the same cell drafts a defaults reorder. **R4 policy** — a `local_only` project whose local rows bounced ≥0.7 at `n≥6` drafts "consider lifting local_only for lane X", and the converse. Each card carries the cells, the rule, and a **replay**: the PRD-37 resolver rerun over the window's delegations under the change, reporting how many resolutions would have differed and to what. Accept = copy the drafted evidence to a commit (R1/R2) or PUT the profile/policy through the PRD-37 routes (R3/R4). Dismiss is stored per user per card key so the card stays quiet until its evidence changes. |
| D8 | Recommendations and floor-crossings draft LESSON CANDIDATES | An accepted card, and any cell crossing `n=5` for the first time, writes a `MemoryShard` candidate with `source = "harness-telemetry"`, `scope = global` at project reach, text in the PRD-16 lesson shape ("For <lane>/<task class> items in <window>, <vendor>:<model> signed off k/n; <runner-up> j/m."), and the cells as its provenance. It enters the review inbox; a human publishes (AL-49). The Lessons page scores it like any other. Nothing is published by this PRD. |
| D9 | The resolver's measured cells gain a window and a band | PRD-37 D7 amended: `fleet_status.measured` and `brief` read the trailing **90 days** by default (`window_days` on the REST read only), and carry the difficulty band. `n` counts within the window. The resolver's lookup is unchanged in shape; a stale cell ages out rather than anchoring a choice forever. |
| D10 | The brief carries the cells for the item's lane and class | `get_item_details.brief.measured_for_lane`: the cells that match this item's lane and task class, for the tiers the brief may suggest. The suggestion's `basis` may cite them ("measured: cheap 7/9 in backend/M over 90d"). The brief suggests, the parent decides (PRD-35 D5) — unchanged. Result payload only. |
| D11 | Rollups are weekly and kept; raw rows age out | `harness_rollups`: one row per cell per ISO week with `finished`, `signed_off`, `bounced`, `median_seconds`, `tokens_in`, `tokens_out`, sampling counts. Rolled nightly and on read when stale. Raw `attempt_telemetry` kept 400 days. The trend chart reads rollups; the recommendation replay reads raw rows within the window. |
| D12 | The org view is the project view over the org's projects | `scope=org` aggregates cells across the org's projects for org admins (PRD-1 authz), with a per-project breakdown on expand. Same rules, same floor; a recommendation at org scope names the projects its evidence came from. |
| D13 | Platform average is hosted-only, opt-in, aggregate-only, k-anonymous | `Organization.telemetry_share` (default false). A nightly job over contributing orgs writes `platform_rollups` per cell per week with `orgs_contributing`. The overlay is served only when a cell has `orgs_contributing ≥ 3` and `n ≥ 20`; otherwise the chart says "no platform average: fewer than three orgs contribute here". Contribution is the rollup, never a raw row, never an org id. The operator console shows the platform view whole. Self-hosted instances have no overlay and the toggle says why. |
| D14 | Exploration is shown, not performed | The page's skew badges and a "cells below the floor" list are the whole of this PRD's answer to bias. A budgeted exploration policy is a separate PRD, opt-in, with its own cost disclosure. |
| D15 | Every number is explainable in one sentence | A count, a rate, a median, a rule. No fitted model. A card that cannot state its rule and cells is not shown. |
| D16 | No manifest change | Everything travels in existing result payloads or over REST. The API-key REST route in D3 is the one new authentication path and is limited to that route. |

### D7 — The replay

The replay is the part that turns a card from an opinion into a claim someone can check: take the window's finished delegations for the project, rerun `Matrix.resolve` for each under the proposed change (a row flipped, a profile reordered, a policy toggled) with the profile and policy that applied, and count the resolutions that differ. The card shows "would have changed 14 of 31 cheap resolutions: 14 from claude:sonnet to gbagent:qwen3.6" and lists them. It does not claim those 14 would have gone better; it says what the change does, which is the only thing a replay can honestly say.

## 6. Data model

`attempt_telemetry` (new, alembic `0111`): `id`, `delegation_id` fk unique, `project_id`, `item_id`, `vendor`, `model`, `binary_version` nullable, `lane`, `tier_requested`, `tier_declared`, `task_class`, `size_band`, `attempt_no`, `sampled` (`first_choice|fallback|explicit|unknown`), `outcome`, `bounce_category` nullable, `claim_to_finish_s`, `turns_used` nullable, `turn_budget` nullable, `wall_seconds` nullable, `tokens_in` nullable, `tokens_out` nullable, `exit_meaning` nullable, `derived_at`, `reported_at` nullable.

`harness_rollups` (new): `project_id`, `week` (ISO), cell keys (`vendor`, `model`, `binary_version`, `lane`, `tier`, `task_class`, `size_band`), `finished`, `signed_off`, `bounced`, `median_seconds`, `tokens_in`, `tokens_out`, `first_choice`, `fallback`, `explicit`, `unknown`. Primary key on the tuple.

`platform_rollups` (new, hosted): the same cell keys without `project_id`, plus `orgs_contributing`. Written only from orgs with `telemetry_share = true`.

`recommendation_dismissals` (new): `user_id`, `scope` (`project|org`), `scope_id`, `card_key` (rule + cell + version), `evidence_hash`, `dismissed_at`. A card whose `evidence_hash` changes is shown again.

`organizations` gains `telemetry_share` bool default false.

`memory_shards.source` gains the value `harness-telemetry`; no column change.

The resolution's winner is recorded on the seat the supervisor mints for the spawn (`Seat.declare` already carries vendor/model/tier; add `chosen_by`), so the server can set `sampled` at outcome time without a new call.

## 7. Acceptance criteria

Each is a test. Sabotage the call, not the model.

1. A delegation that finishes produces exactly one `attempt_telemetry` row with lane, tiers, vendor, model, task class, size band, attempt number, outcome, bounce category and claim-to-finish; an open or expired delegation produces none. Sabotage: write the row at claim and the expired case fails.
2. `POST /api/fleet/attempts` with a supervisor's API key enriches the row (version, turns, wall, tokens) and is idempotent; a session token is refused; a key without `fleet_status` scope is refused; a second post does not duplicate.
3. Null token fields render as "not reported" on the page and in the rollup, never as zero. Sabotage: coalesce to 0 and the test fails.
4. `sampled` is `first_choice` when the declared vendor/model equals the winner recorded at spawn, `explicit` when the spawn named an adapter, `fallback` when it equals the recorded runner-up after an installed drop, else `unknown`.
5. Rollups: a week with three finished attempts in one cell rolls to one row with the right counts and median; two cells in one week roll to two rows; rerolling is idempotent.
6. A rate below `n=5` is served with `below_floor: true` and the page draws it grey; a rate with more than 80% one sampling reason carries the skew badge. Sabotage: pool two cells to cross the floor and the per-cell test fails.
7. Versions: two binary versions of one harness are two cells; the page defaults to the current version and shows both on request; a recommendation names the version.
8. R1 fires on an `unverified` row with ≥10 finished and signed-off ≥0.8 and drafts evidence text naming the items; it does not fire at 9 finished, or at 0.79, or when pooled across bands would have reached the threshold but no single band does.
9. R2 fires on a `verified` row with ≥6 finished and signed-off ≤0.25; R3 on a beaten default at `n≥8`; R4 on a `local_only` project whose local rows bounced ≥0.7 at `n≥6`. Each card carries rule, cells and replay.
10. The replay reruns the PRD-37 resolver under the change and reports how many resolutions differ and to what; with no change it reports zero.
11. Accepting R3 or R4 PUTs through the PRD-37 profile/policy routes and nothing else; accepting R1 or R2 copies drafted evidence and changes no server state. Dismiss hides the card for that user until its `evidence_hash` changes.
12. An accepted card and a first crossing of `n=5` each create a `MemoryShard` candidate with `source = "harness-telemetry"` in the review inbox; nothing is published; a second crossing of the same cell creates nothing.
13. `fleet_status.measured` and the brief count only the trailing 90 days and carry the band; an attempt 91 days old is absent from `n`. `brief.measured_for_lane` carries only the item's lane and class.
14. Org scope aggregates the org's projects for an org admin and refuses a non-admin; a project member sees only project scope.
15. Platform overlay: a cell with two contributing orgs is not served; with three and `n≥20` it is; the served payload carries no org id and no project id; an org with `telemetry_share = false` contributes nothing. Self-hosted returns `platform: null` with the reason.
16. The MCP manifest's measured token count is unchanged (`test_mcp_footprint`).
17. Operating loop, on the deployed instance: after the walk items of PRD-36/37 and ten more delegations, the Harness page shows the gbagent backend/cheap cell above the floor with its skew badge, a grey qwen-code cell, and either an R1 card for qwen-code or the reason none fired; accepting nothing, the lesson inbox holds one candidate from the floor crossing. Recorded as `note` evidence.

## 8. Phasing

**PR 1, the record.** `attempt_telemetry`, server derivation at outcome, `POST /api/fleet/attempts` with API-key auth, the supervisor's exit report (gbfleet parses the child's result record where the vendor prints one), `sampled` from the seat's recorded winner, the 90-day window and band on `measured`. Criteria 1–5, 13, 16.

**PR 2, the page.** Rollups, the Harness page under Observe for a project: trend per cell, floor, skew, versions, cost proxy; `doctor` prints the same cells. Criteria 6, 7.

**PR 3, recommendations and lessons.** Rules R1–R4, the replay, accept and dismiss, lesson candidates. Criteria 8–12.

**PR 4, org and platform.** Org scope, `telemetry_share`, the platform rollup job, the overlay and its floor, the operator console view. Criteria 14, 15. Then criterion 17.

## 9. Risks and open questions

### Risks

- **Reviewer variance.** Signed-off depends on who reviewed. A strict reviewer on one lane depresses every harness there equally, which the per-lane cells absorb, but a reviewer who favours one vendor's style is a bias no cell can see. Mitigation: the org view shows rates per reviewer on expand, labelled as a diagnostic, not a rule input.
- **Gaming by declaration.** Cells attribute by what the child declared (GRPH-732). A child that declares a vendor it is not is a lying child; the supervisor's exit report carries the adapter it launched, and a mismatch is flagged on the row, not silently trusted either way.
- **Thin cells forever on small instances.** A one-person self-hosted box may never cross the floor on anything but its favourite. The page must be honest about that rather than decorative; the platform overlay is the hosted answer, and there is no self-hosted one.
- **Rules that are wrong.** R1–R4 are first guesses at thresholds. They are drafts to a human, so a wrong rule costs attention, not a bad spawn; thresholds are constants in one module and the page shows them.

### Open questions

1. Should the exit report be posted by the supervisor (D3) or should the child be told to attach it to its own last `update_item` evidence? The supervisor knows the truth and the child might not report; the child path needs no new auth. D3 chooses the supervisor; the grill may disagree.
2. Is a 90-day window right for the resolver, or should the window be a profile setting? D9 picks a constant so the explanation stays readable.
3. Does the platform overlay need a per-cell opt-out beyond the org toggle (e.g., an org willing to share backend cells but not frontend)? D13 says no for v1.
4. Should an org admin be able to accept an R3 card on a member's behalf? D7 says a profile is its owner's; the card at org scope is informational.

## 10. Prior art

- PRD-37 D7/D8: measured cells with `n`, the explained resolution this PRD reads and extends.
- PRD-35 D5, D9: the brief suggests, the parent decides; outcomes stored at the event.
- PRD-16 and `docs/design-observe-lessons.md`: lessons as candidates → review → published, effectiveness as a computed judgement.
- PRD-33/34: Observe as the place derived state is read, polled at the server's cadence.
- PRD-1: orgs, hosted mode, the operator console.
- `docs/fleet-adapters.md` "Which model, and what that was measured on": the one measured comparison the repository holds, and its caveats — thin evidence read as thin.
