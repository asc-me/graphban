# PRD-41 — The capability matrix: grading models by function set, and learning it across users

**Ledger id:** GRPH-P41 — created in the ledger 2026-09-08. The ledger is the source of truth; this file is a review copy and must agree with it (`backend/tests/test_prd_sync.py`). Committing it means regenerating `docs/prd-index.json` with `scripts/gen_prd_index.py`.
**Status:** approved — v1.1 on 2026-09-09. Approval was earned at v1.0 by finishing the first grill round (eight questions, every answer graded, every dimension closed); a second round of seven sharpening questions was answered the same day and is absorbed as v1.1 (marked *grill 2:* below). Two first-round answers changed a decision: D16/D20's scoring order and D11's accept-time redaction. The second round changed none, and made three refusals explicit: no tokenizer coefficient, no damping of the platform prior, no automatic block when R5 is ignored.
**Depends on:** PRD-37 (the matrix, profiles, policy, the explained resolution and its `measured` cells) · PRD-38 (attempt records, rollups, the Harness page, R1–R4 as drafts, org scope and the opt-in platform overlay) · PRD-35 (the delegation record: requested tier, declared vendor/model, outcome at the event) · PRD-36 (bound seats) · PRD-16 (lessons) · PRD-1 (orgs, hosted mode, the operator console) · the deployment sync credential (Settings → Deployment → Sync) for self-hosted contribution
**Touches:** `backend/app/services/harness.py` (capability derivation beside `task_class` and `size_band`) · `backend/app/services/harness_rules.py` (R5) · `backend/app/services/delegation.py` (capabilities on the brief and the record) · `backend/app/services/platform.py` (capability-keyed platform rollups, the published prior) · `backend/app/routers/harness.py` and `fleet.py` (REST only) · `backend/alembic/versions/` · `fleet/src/gbfleet/matrix.py`, `matrix.toml` (optional prices), `supervisor.py`, `worktree.py` (diff shape in the exit report; capability-aware scoring and the measured cost axis) · `web/src/features/harness/` (the capability grid) · `docs/fleet-adapters.md`, `docs/api-reference.md`
**Complemented by:** the Build → Harnesses design in the `graphban-local` Claude Design project (`pages/build-harnesses-proposed`) · `gbfleet doctor` · the PRD-38 §7 acceptance walk, whose 0/5 cell is the motivating example of a number that needed a finer axis to mean anything

---

## 1. Overview

PRD-37 gave the supervisor a table of what runs which tier, and PRD-38 made that table learn from what happened: one row per finished attempt, rolled into cells keyed on vendor × model × lane × tier × task class × size band. That is enough to say "gbagent:qwen3.6 signs off 82% of backend/docs/S" and not enough to say the thing an operator actually needs, which is closer to *"this model is fine at a localised fix and a docs change, cannot write a migration that downgrades, and never bounces a review it should."* Backend and frontend are the two lanes the ledger can see. The competence differences that decide whether a cheap model is worth trying live one level down, in what kind of function the work asks for.

This PRD names that level. It defines a **capability set** — six families, twenty-four function sets — derived from what an attempt touched, what its diff did, and how it ended, so every attempt already flowing through PRD-38 tags itself without anyone labelling it. Cells re-key on vendor × model × **capability** × size band. Tier drops out of the key because tier is the question the matrix exists to answer; lane becomes a family, not a key. A model's competence becomes a grid a person can read across the top ("where is this model weak?") and down the side ("who is best at migrations?"), and the resolver reads the same grid to pick a harness for the capabilities an item is about to need.

Then it makes the grid **corrective across users**. One project's traffic is thin and skewed toward whatever the resolver already prefers; PRD-38 shows that honestly and cannot fix it. This PRD adds three sources that do: the org's other projects (PRD-38 D12, re-keyed), the hosted platform's opt-in aggregate (PRD-38 D13, re-keyed and floored the same way), and a **probe panel** — a fixed set of the instance's own closed items with a red sabotage on record, replayed against a model when it is first installed or changes version, so a new model is graded before it has earned any traffic. The committed matrix becomes the **prior**; the cells are the **posterior**; a rule (R5) drafts a matrix change when the two disagree at a sample size that means something, and a published platform snapshot gives a fresh install a starting prior it did not have to earn alone.

**One sentence:** every attempt tags the function sets it exercised; cells key on those; local evidence outranks the org's, which outranks the platform's, which outranks the committed row — and each layer says which one it is. The user's four weights are unchanged; what they weigh is now the work's own function sets, cost is what a signed-off outcome took rather than what an attempt took, a budget is both a project cap that filters and a user target that scores (D20), and a better harness that is not installed is a card, not a silence (§7). The pick itself is one pipeline over grid, policy and profile, and every stage is on the record (§7.7).

### 1.1 What this is not

- **Not a benchmark leaderboard.** No cell is served without its `n`, its floor, its sampling mix and its window, and no number here is a claim about a model in general. It is a claim about this model, at this version, on this kind of function, in this instance's repositories, over 90 days.
- **Not a fitted model.** Every grade is a count, a rate, a median, or a rule over them (PRD-38 D15). The "corrective" mechanism is a rule that proposes a matrix change and a human who applies it, never a weight that moves on its own.
- **Not an exploration policy.** Probes are deliberate and bounded; the page still shows skew rather than correcting for it (PRD-38 D14). A budgeted exploration policy remains a separate PRD.
- **Not a new MCP tool.** The manifest has seventeen tokens of headroom. Everything here travels in existing result payloads or over REST.

---

## 2. Problem

### 2.1 The lane is the finest axis, and it is the wrong one

`lane_for` says `web/**` is frontend and everything else is backend. `checklist_for` adds migration, mcp_tool, frontend and docs from the same paths, else `general`. So a model that writes a clean service function and a broken alembic downgrade lands both in `backend`, and the cell reads 50% — which is true and tells nobody what to do differently.

### 2.2 The 0/5 cell that meant nothing

PRD-38's walk produced `gbagent:qwen3.6 backend/cheap/docs/S n=10 rate=0.10`. Nine of the ten bounces measured the supervisor's plumbing, not the model, and the page could not separate them because it had no axis for *what went wrong where*. With protocol compliance (E1) as its own function set, those nine land in a cell that says "never reached a branch" and the docs cell says "1/1 signed off, below the floor", which is the truth.

### 2.3 One project cannot fill a grid

Twenty-four function sets × three size bands × the models an instance runs is several hundred cells. At the PRD-37 floor of `n ≥ 5`, a single project with a few dozen delegations a week fills a handful. Without aggregation the grid is mostly grey, and grey reads as "unknown" only if the page insists on it.

### 2.4 The samples are the resolver's habits

PRD-38 §2.2 already names this: a cell sampled 100% first-choice tells you how the winner did, not how the loser would have. Nothing in the current system ever runs the loser on purpose. A new model, or a new version of an old one, has no traffic and therefore no grade, and no grade means the resolver never picks it, which means it never gets a grade.

### 2.5 Review is a function set, and nobody grades it

The fleet reviews itself (PRD-39). A model that signs off everything and a model that bounces everything both finish reviews; the `signed_off / finished` rate says nothing about whether their verdicts were right. Bounce precision, recall and reason quality are gradeable, and today they are not graded.

### 2.6 The matrix is a fact table with no way to be corrected by evidence

`matrix.toml` moves a row to `verified` or `failed` by a commit naming an item. R1 and R2 draft those commits from cells. There is no notion of a row's *prior* competence per function set, so a row that is verified for docs is verified for everything, and a cell that contradicts a row has no path back into the file except a human noticing.

---

## 3. Goals

1. Every finished attempt carries a set of capability tags derived by the server from the item's touchpoints, the attempt's diff shape and its outcome, with no human labelling and no LLM classification.
2. Cells key on vendor × model × capability × size band, multi-label, rolled up by family until a leaf clears the floor; the Harness page shows the grid.
3. The resolver reads the capabilities the item is about to need and scores quality from those cells, falling back family → org → platform → committed prior, and its explanation names which layer decided.
4. Review competence (precision, recall, reason quality) is measured from verdicts that were later contradicted or confirmed.
5. A model is graded before it earns traffic: a probe panel from the instance's own closed items runs on install and on version change, labelled as probe and never mixed into natural rates.
6. Evidence corrects the prior: R5 drafts a matrix change when a cell disagrees with its row at `n ≥ floor`; the platform publishes a capability snapshot that a fresh install uses as its starting prior.
7. Cross-user aggregation keeps PRD-38 D13's floors and privacy posture unchanged, and adds nothing that carries a path, an item, a reviewer or a repository name off the instance.

### Non-goals

- Grading humans. Only attempts by agents on delegation records are measured.
- A capability a person sets by hand on an item. Tags derive; a hand label would be the taste PRD-37 D1 keeps out of fact tables.
- Per-capability profile weights. A profile stays four axes; capabilities feed the quality axis.
- Public publication of any instance's probe panel text. Panels are local; only capability labels and outcomes leave.

---

## 4. Key decisions

| # | Decision | Why |
|---|---|---|
| D1 | **The unit stays the attempt**; an attempt carries a SET of capabilities, and contributes to every cell it names | Multi-label is what fills cells fast enough to matter and is honest: a feature slice touching a migration and a route did exercise both. A single primary label would force a guess about which one "counted". Rates stay `signed_off / finished` per cell with no exclusions. *Grill:* a shared attempt therefore moves every cell it names, and an E1 failure does lower the A4 cell — by design, since that attempt failed on an item that needed A4. The resolver does not de-correlate. What the grid adds is the reason read: the E-family row carries the failure's shape, so the page can say "A4 0.40, of which 4 of 7 bounces were E1". No de-correlation in this PRD; a stated limit, like skew. *Grill 2:* E1 failures are not excluded from other cells — the deflation is a feature. A model that cannot finish the loop is not good at migrations in any sense an operator can use; the E-family row beside the deflated cell is the whole of the fix. |
| D2 | **Tags derive from three things the ledger already has or the supervisor already sees**: touchpoint paths (as `checklist_for` does), the diff shape posted at exit (files added/modified/deleted/renamed, test files touched, net lines, layers touched), and the outcome record (bounce category, `released`/`expired`, evidence kinds) | No LLM in the classifier, no free-text field a threshold could later be wired to (PRD-38 D2's rule for bounce category applies here in full). The derivation is a function in `harness.py` beside `size_band`, unit-tested against fixed diffs, and its coverage is a visible number: attempts that derive to no leaf land in their family's `other` cell, shown with its count. *Grill 2:* a hybrid file (a `common.py` touched by a migration and a route) tags every leaf whose pattern matches, on purpose; the diff shape carries the intent paths cannot. The derivation defines no priority between leaves — it defines coverage, and the coverage number is the guard against the heuristics rotting. |
| D3 | **The cell key becomes vendor × model × binary_version × capability × size_band**; tier and lane leave the key | Tier is what the matrix answers; keying on it made the grid answer "how did cheap do when we asked for cheap". Lane is the parent family of most capabilities and is served as a rollup. `task_class` is retired in favour of capability; its four values map onto A4, B2, B5 and E3 so no history is lost. |
| D4 | **Six families, twenty-four function sets (§5)**; leaf cells are shown when `n ≥ 5`, else the family rollup is shown with the leaf greyed at its `n` | The floor is PRD-37's `MIN_SAMPLE`, unchanged. Rolling up by family is what lets a page say something true about a model on day three; showing the leaf grey beside it is what stops the rollup being mistaken for the leaf. |
| D5 | **The resolver reads the item's capabilities**, derived from its touchpoints at `delegate` time and carried on the record and the brief; quality for a row is the mean of its cells over those capabilities, each cell taken from the first layer that clears the floor: project → org → platform → committed prior | The order is "closest evidence wins". Each layer is labelled in the explanation (`quality: 0.71 from org cells (2 of 3 capabilities), platform prior for H1`), because a score assembled from four sources that did not say so would be the hook pack's failure mode again. The item's capabilities at `delegate` time can only come from touchpoints (there is no diff yet); the exit-time set may be larger, and the record keeps both. *Grill 2:* the platform prior is used whole, with no damping factor — a damped prior is still a prior, only harder to read. Its protection is that it is labelled `platform` and dated in every explanation until a local cell replaces it at five attempts, and that the fetch can be turned off. |
| D6 | **Review competence is measured against later contradiction**: a sign-off followed within 14 days by a bug item or a bounce on overlapping touchpoints is a *miss*; a bounce followed by CI `suite_green` on the same head and a human override is a *false bounce*; a bounce whose reason maps to `other` is *unclassified* | F1–F3 need ground truth the ledger does not have at verdict time, and later events are the only honest source. The window and the overlap rule are constants, not settings, so every card carrying them says the same thing. A reviewer's cells key on the CAPABILITIES OF THE WORK IT REVIEWED, so "misses migrations" is expressible. *Grill:* a filed bug on overlapping touchpoints IS the contradiction whether or not it is fixed yet: the check writes `miss (unconfirmed)` at filing, flips to `miss` when the bug closes as fixed, and to `withdrawn` (leaving the cell) if it closes as not-a-bug. F cells show confirmed and unconfirmed as two numbers; nothing waits on the repair. *Grill 2:* attribution is by touchpoint overlap and nothing finer, so a miss can be wrong; closing the bug as not-a-bug or `unrelated` withdraws the check, and because checks are rows rather than counters the withdrawal recomputes the reviewer's cells forward and back within the 90-day window. Beyond that the miss stands, and the F2 label says "by touchpoint overlap". |
| D7 | **The probe panel is local, drawn from the instance's own closed items that carry a red `sabotage` evidence** (tests_failed ≥ 1), one to three items per leaf capability, chosen by the operator from a list the server proposes; runs through the ordinary `delegate` → bound seat → review path on a scratch project, `sampled = probe` | A red sabotage is the one guarantee that the item's test discriminates, which is what makes the item a probe rather than a task. Using the real delegation path means a probe exercises E1 too, and the review is a real independent review. Probe attempts contribute to cells with their own sampling label, and the page never pools probe and natural rates in one number: a cell shows both, each with its `n`. *Grill:* probes obey the same `n ≥ 5` floor per cell — below it a probe cell is grey with its `n`, feeds no layer of D5 and fires no R5; there is no bypass. An instance too small to field five red-sabotage items per leaf runs the panel at FAMILY level and gets a family-level probe cell. *Grill 2:* nothing about a probe leaves the instance except what any attempt sends — the D11 field set plus `sampled = probe`. The panel, its items, their diffs and the sabotage tests are local and never transmitted; the platform learns that a probe ran on A4 and how it went, never what it was. |
| D8 | **Probes run on two triggers only**: a harness or model first resolved by the matrix (no natural cell for it), and a `binary_version` or model change on an existing row; never on a schedule | A scheduled probe is an exploration budget by another name and belongs to that PRD. A probe on version change is what answers PRD-38's open item that a supervisor-side change is invisible to the cells: the panel result at the new version is the before/after. *Grill:* the trigger is a change in what the CHILD DECLARES — vendor, model string, `binary_version`; a supervisor-side patch changes none of those and starts no cell and no probe. If the operator ignores the suggestion, the new version's cells fill from natural traffic with the prior inherited and labelled `prior (inherited from <previous version>)`. |
| D9 | **R5 — reprior**: a cell whose rate at `n ≥ 5` differs from its committed row's per-capability prior by ≥ 0.3 drafts a matrix evidence line for that capability; a leaf with no prior drafts one from the cell; probes count toward R5 with their label on the card | Corrective means the evidence has a path back into the fact table, and the path is a human commit, as R1 and R2 already are. The margin matches R3's, and the card carries the same replay, siblings and hash rules. R5 never edits `matrix.toml`; it drafts the line. *Grill 2:* nor is there a safety override that blocks a harness on a threshold — routing that moves on a threshold is the fitted-model door D15 keeps shut. If a human ignores R5 while a verified row collapses, R5 returns on every evidence change, the card sits on the page the operator opens to change anything, and the measured quality axis already scores that row near zero for the capability, so the resolver stops picking it under any profile that weights quality at all. Status is a fact; the score is what routes. |
| D10 | **The committed matrix gains per-capability evidence, not per-capability rows** | A row stays harness × model (× lane during the PRD-39 tail). `evidence` entries gain an optional `capability` field. A row's status for a capability is the newest evidence entry naming it, else the row's status. This keeps one row per pair, keeps the file readable, and lets D5's prior be read without a schema change to the resolver's row match. *Grill:* neither split rows nor a status map — a row verified for A4 and failed for B4 is ONE row with two evidence entries naming those capabilities; R5 drafts an evidence entry and never flips the row's `status`; `doctor` prints the row status and every per-capability reading beside it. |
| D11 | **Cross-user aggregation is PRD-38 D12 and D13 re-keyed, with one addition**: a self-hosted instance may contribute its capability rollups to the hosted platform over the existing deployment-sync credential, opt-in, rollups only | D13's three floors (≥ 3 orgs, aggregate `n ≥ 20`, no org above 60%, an org counting only with `n ≥ 5` of its own), the week granularity, two-decimal rounding and `n` bands all hold. What crosses the wire is `capability, size_band, vendor, model, binary_version, week, finished, signed_off, sampling counts` — a capability label is a closed enum from §5 and carries no path, no item, no repository name. Self-hosted contribution is the same toggle as `telemetry_share`, worded for a self-hosted operator, and the sync status page shows the last contribution and its row count. *Grill:* the platform ACCEPTS any contribution and SERVES nothing under the three floors, so a unique model string never appears in the overlay or the snapshot; and at accept time a model string seen from fewer than three contributing instances is stored under its vendor with the model redacted to `other` until a third instance reports the same string — the contributor's sync page says so. |
| D12 | **The platform publishes a capability snapshot** — the served aggregate at week granularity as a versioned JSON — which any instance may fetch as its starting prior, and which `gbfleet doctor` can print beside the local grid | A fresh install has no cells and today no prior beyond `matrix.toml`'s status. The snapshot is the platform's `served` payload, so it can carry nothing the overlay does not already show. It is a prior, labelled `platform` in every explanation, and it is outranked by any local cell at the floor (D5). No instance is required to fetch it; a self-hosted instance with the toggle off resolves on its own cells and the committed matrix, as it does today. |
| D13 | **Version is part of every key and every probe** | `binary_version` stays in the cell key (PRD-38 D5). A model string change or version change starts new cells that inherit the prior, triggers D8's probe, and the page shows the previous version's cell beside the new one for the overlap window rather than drawing one trend through two versions. |
| D14 | **The Build → Harnesses page is where this lands** (the `graphban-local` design) | The grid, the probe panel status, R1–R5 cards and the profile/policy editors belong on one screen because a change in a model's measured competence is a request to move a control. Observe → Harness stays as the time-series view. |
| D20 | **A token budget is both a constraint and a preference, and the same number may appear in both** | Policy gains `caps` — `per_attempt_tokens`, `per_item_tokens`, `per_period_tokens` with `period` — and a cap is a FILTER: a row whose expected tokens-to-sign-off for the item's capabilities exceeds what the cap leaves is dropped, and the refusal names the cap. The profile gains `budget_tokens`, a soft target per sign-off: rows at or under it score 1.0 on cost, rows above it decline toward 0.2 at twice the target, and nothing is removed. The project says what may not be spent; the user says what they would rather not spend. When both name a number the cap applies first, as every policy does (PRD-37 D4). A vendor that reports no tokens cannot be capped: under a cap such a row is dropped with the reason `tokens not reported`, because a cap you cannot measure is not a cap. *Grill:* the curve is linear from 1.0 at the target to 0.2 at twice the target, floored at 0.2, and replaces rank-scaling whenever a target is set. When EVERY eligible row is unreporting under a cap, the cap empties the resolution and the refusal names it (`per_item_tokens: no eligible row reports tokens`) exactly as `local_only` with no local row does; there is no silent fallback to `cost_class`, which would enforce the cap only on honest vendors. The card that fires is R6-shaped: "this cap is unenforceable on the rows this project allows", listing the rows and their reporting shares. |
| D21 | **The selection is one pipeline over the grading grid, the policy and the profile, and its explanation carries every stage** | `rows for the item's capabilities → policy (allowed_harnesses, local_only, caps) → profile (defaults, excludes) → installed and served → score (quality from the capability cells by layer, cost from tokens-to-sign-off against the budget target, latency, locality, by the user's weights) → tie-break (verified > unverified, defaults order, matrix order)`. Every stage records what it dropped and why; the winner's four axis values and their sources are on the record. This is PRD-37 D5 with three inputs it did not have — capabilities, caps, the budget target — and no change to its shape. |
| D15 | **No manifest change** | Result payloads and REST only. `get_item_details.brief` gains `capabilities` (a short list of enum strings) inside its existing `measured_for_lane` cap; the bound is re-asserted by test. |
| D16 | **Cost is measured as tokens to a signed-off outcome, per capability, and refines `cost_class` rather than replacing it** | `cost_class` stays the prior (local 1.0 / cheap 0.6 / frontier 0.2). Where a cell's cost proxy is comparable (PRD-38 D11: ≥ 80% of attempts reported tokens), the eligible rows with a comparable number are rank-scaled onto the same span and the explanation says `cost: measured 84k/sign-off`; rows without stay on the class and say `cost: class`. The numerator includes bounced attempts because a model that bounces most of its migrations spends its tokens and the reviewer's twice, and a per-attempt number hides exactly that. *Grill:* rank-scaling applies ONLY when the profile sets no `budget_tokens`; when a target is set, the D20 curve replaces it entirely, so 30k and 300k against a 50k target score 1.0 and 0.2 rather than both "high". *Grill 2:* raw reported tokens stay the proxy across vendors with no tokenizer-efficiency coefficient — that would be a guess wearing a fact's clothes. Where a row carries a price (D19), currency is the honest cross-vendor comparison, because the price already absorbs the tokenizer. |
| D17 | **Availability has a measured cost, and R6 drafts the remedy** | Installed is checked last (PRD-37 D5) precisely so the launch post records what would have won. When ≥ 6 resolutions on a capability in the window dropped as `not installed` a row whose measured grade (org, platform or probe — never the committed prior alone) beats the winner's by ≥ 0.3, R6 drafts "install or serve X for A4" with the count, both grades and their layers, and the remedy line `doctor` already prints. Uninstalled rows appear on the grid greyed with their layer label, never blank. |
| D18 | **`defaults` stays one ordered allowlist; with quality per capability, order is a tiebreak and membership is the decision** | One list cannot say "best at docs, worst at migrations" and does not need to: the score does. So R3 drafts a reorder only when the top default is beaten at the FAMILY level across ≥ 2 capabilities; a per-capability beat is shown on the grid and drafts nothing. Per-family default lists are rejected: taste per function set is what the measured axis replaces (PRD-37 D1). |
| D19 | **Prices are optional committed facts on the matrix row, and money appears only where they exist** | `price_per_mtoken_in` / `_out` on a row (local rows 0). When both rows in a comparison carry prices the explanation adds expected spend per sign-off in currency; when one does, currency shows for that row only and the axis still ranks on tokens. Prices in the matrix rather than the profile because a price is a fact with a reviewer, not a weight. |

---

## 5. The capability set

Six families. "Detected from" is the derivation D2 runs at the outcome event; "graded by" is the signal the page and the rules read for that set, because `signed_off / finished` alone is too blunt for several of them. Every cell still shows the plain rate with its `n`; the graded-by column is the *additional* number a card may cite.

| Id | Capability | What it tests | Detected from | Graded by |
|---|---|---|---|---|
| **A** | **Change shape** | | | |
| A1 | Localised fix | One failing behaviour, ≤ 2 files, cause known | item type `bug`; diff touches ≤ 2 non-test files and ≥ 1 test | signed-off; first-attempt rate (`attempt_no = 1`) |
| A2 | Feature slice | New service function + route + type + test as one coherent change | diff adds files in ≥ 2 layers (§5 B) | signed-off; scope-bounce share |
| A3 | Behaviour-preserving refactor | Rename/move/extract with existing tests untouched and green | diff has renames/moves, test bodies unchanged | signed-off; quality-bounce share |
| A4 | Schema and migration | Model, alembic up and down, both engines, the range doc | `alembic/versions`, `models/__init__` | signed-off; Postgres job green |
| A5 | Deletion and cleanup | Remove dead code without breaking imports or doc guards | net-negative diff, ≥ 1 file deleted | CI green; process-bounce share |
| **B** | **Layer** | | | |
| B1 | REST and contract surface | Route, pydantic shape, api-reference row and count sentence | `routers/`, `docs/api-reference.md` | signed-off; doc-sync test failures |
| B2 | MCP tool surface | Schema under the manifest ceiling; reply-side changes | `mcp_server.py`, `tool_tiers.py` | signed-off; footprint test result |
| B3 | Service and domain logic | Invariants, error taxonomy, hints | `services/*.py` with no other layer | signed-off; quality-bounce share |
| B4 | Persistence and queries | SQLAlchemy, indexes, N+1, both engines | `models/`; services with `select(` deltas | one-engine-red rate; median seconds |
| B5 | React component and state | Hooks, optimistic updates, the api.ts/queries.ts invariant | `web/src/features`, `web/src/lib` | signed-off; typecheck + test pass |
| B6 | Visual fidelity | Tokens, layout, responsive, matches the design | `web/src/components/ui`, `*.css` | signed-off (human review is the only signal) |
| B7 | CLI, process and git | argparse, subprocess, worktrees, detached launch | `fleet/src/gbfleet/*`, `cli/` | signed-off; fleet suite green |
| B8 | Infra, CI and config | Workflows, docker, env, release stamps | `.github/`, `docker*`, `pyproject*` | outcome of the next `main` run |
| **C** | **Verification behaviour** | | | |
| C1 | Discriminating tests | A test that fails when the change is reverted | evidence kind `sabotage` with `tests_failed ≥ 1` | sabotage present AND red; no-op sabotage rate |
| C2 | Red-to-green loop | Runs the right suite, reads the failure, fixes the cause | test runs in the attempt; turns | tests-bounce share; turns to green |
| C3 | Reproduce first | Writes the repro before the fix on a reported bug | `bug` items; a test added before the implementation commit | first-attempt rate on `bug` items |
| **H** | **Reasoning demand** | | | |
| H1 | Concurrency and idempotency | Leases, TTLs, retries, duplicate-safe writes | paths or symbols naming lease/claim/idempotency/ttl | quality-bounce share; later-regression rate |
| H2 | Authorization boundaries | Refusal tests, scope, 404-not-403 | `security/authz.py`, `keys.py`, refusal tests | signed-off; security bounces |
| H3 | Cross-engine semantics | SQLite vs Postgres: JSON, ordering, timezones | both-engine CI split | one-engine-red rate |
| H4 | Long-context coherence | ≥ 6 touchpoints; follows an existing pattern across the tree | size band `L` | signed-off at L against the same model's S |
| H5 | Spec fidelity | Exactly the acceptance criteria, nothing beside | every attempt | scope-bounce share |
| **E** | **Operating the loop** | | | |
| E1 | Protocol compliance | Register, claim, heartbeat, commit, push, review, no forbidden tools | outcome `released`/`expired`; no branch; malformed tool calls in the exit report | released + expired share; branchless attempts |
| E2 | Commit hygiene | Explicit paths, no stray files, a truthful message | diff includes instruction or state files | process-bounce share |
| E3 | Prose and docs | Docs lane, sync guards, PRD sections | `docs/**`, `*.md` | signed-off; generator-guard failures |
| **F** | **Review competence** | | | |
| F1 | Bounce precision | A bounce a human or CI would also have given | reviewer attempts; later CI or human verdict (D6) | agreement rate |
| F2 | Bounce recall | Signs off nothing that regresses | sign-offs followed by a bug on overlapping touchpoints within 14 days (D6) | miss rate |
| F3 | Reason quality | A categorisable, actionable reason | `bounce_category != other` | unclassified share |

Rules for the set: an id never changes meaning once shipped (a cell's history depends on it); a new leaf is added under an existing family; retiring one merges it into `other` for its family with the change dated in this file. The enum is served on `GET /api/harness` so the page and `doctor` never hard-code it.

---

## 6. Gathering the telemetry across users

This section is the "how do we build the model over time" answer. Four layers, each of which the resolver can read and each of which the explanation names.

### 6.1 Layer 0 — the attempt (one instance, one project)

Unchanged from PRD-38 D1–D3 with two more fields in the supervisor's exit post: `diff_shape` (`{files_added, files_modified, files_deleted, files_renamed, test_files, net_lines, layers}` computed by `worktree.reap` from `git diff --stat --name-status` against the base) and `tool_errors` (the count of malformed or refused tool calls the adapter observed, when it can). Both are runtime facts only the supervisor has; both follow the merge rule and the null-is-not-zero rule. The server derives `capabilities` at the outcome event from touchpoints + diff shape + outcome, stores the list on the row, and expands it into one rollup row per capability per week.

### 6.2 Layer 1 — the organisation

PRD-38 D12 re-keyed: `scope=org` aggregates capability cells across the org's projects for org admins, per-project breakdown on expand, same floor. A recommendation at org scope names the projects its evidence came from. Nothing new is stored; the query changes key.

### 6.3 Layer 2 — the platform (hosted, opt-in) and self-hosted contribution

PRD-38 D13 re-keyed, floors unchanged. Two additions:

- **Self-hosted instances may contribute.** The deployment-sync credential already lets a self-hosted instance talk to the hosted service. A nightly job, on when the operator turns on `telemetry_share`, POSTs the instance's capability rollups (never raw rows) to `POST /api/platform/contributions` on the hosted service, which treats the instance as one contributing org for every floor. The payload is the D11 list; the response is the count accepted and the snapshot version currently published. The sync status page shows the last contribution, its row count and the floors it did or did not clear. Turning the toggle off stops contribution AND recomputes the affected platform rollups without that contributor, as D13 already promises for hosted orgs.
- **What never crosses.** Paths, touchpoints, item ids, reviewer ids, project or repository names, bounce reasons, diff content. A capability label is one of the §5 ids. `binary_version` and model strings are what the harness prints and are already in the hosted overlay. The doc and the toggle copy say "resists casual re-identification", never "anonymous", exactly as D13 does.

### 6.4 Layer 3 — the published prior

Nightly, after the platform roll, the hosted service writes `capability_snapshot` — the served aggregate per vendor × model × binary_version × capability × size_band × ISO week with `n` as a band — versioned by date and fetchable at `GET /api/platform/snapshot` by any instance with a sync credential (hosted orgs read it in-process). An instance stores the snapshot it last fetched in `capability_priors` with `source = platform` and `snapshot_at`. The resolver reads it as D5's fourth layer; `doctor` prints it beside the local grid with the snapshot date; the Harness page shows it as the platform average it already shows, now per capability. An instance that never fetches resolves as it does today.

### 6.5 How it corrects

- **Locally**, a cell at the floor outranks every prior for that capability (D5), so an instance's own experience always wins where it has any.
- **In the file**, R5 (D9) turns a disagreement between a cell and its row's prior into a drafted evidence line, and a human commit moves the fact. The matrix therefore trails the evidence by exactly one review, which is the same distance R1 and R2 already keep.
- **Across versions**, D13 starts fresh cells and D8 runs the panel, so a regression in a new model version shows up as a probe cell before the resolver has sent it real traffic, and the previous version's cell stays beside it for comparison.
- **Across users**, the snapshot lifts a fresh install's prior from "the committed row says unverified" to "twelve organisations measured this model at 0.78 on migrations over the last quarter", labelled as such, and the moment the install has five attempts of its own on migrations, its own number takes over.

### 6.6 What stays unfixable, and is said so

Skew. A platform aggregate of first-choice samples is a bigger skewed sample. The badge travels with every cell at every layer; probes are the one deliberate counter-sample and they are labelled. Difficulty is still proxied by size band. Vendors that print no tokens still show "not reported". None of these change with more users; more users make the numbers narrower, not truer, and the page says so where it shows them.

---

## 7. Fit with preferences: cost, token utilization and available harnesses

PRD-37 fixed the shape of a preference: `defaults` (an ordered allowlist), four weights (`cost`, `quality`, `latency`, `locality`), `excludes`; policy filters before any score; installed is checked last so the refusal can say what would have won. This PRD keeps that shape whole. It changes what two of the four axes are computed from, adds one card about what is not installed, and states plainly what a single ordered list can and cannot express once quality is known per function set.

### 7.1 The four axes, and what feeds them now

| Axis | Today (PRD-37 D6/D7) | With PRD-41 |
|---|---|---|
| `quality` | signed-off rate per vendor × model × lane × tier at `n ≥ 5`, else `unmeasured` | mean over the item's capabilities of the first layer that clears the floor — project, org, platform, committed prior (D5); `unmeasured` only when no layer has the cell, and the explanation names the layer per capability |
| `cost` | `cost_class` only: local 1.0, cheap 0.6, frontier 0.2 | `cost_class` as the prior, refined by measured tokens-to-sign-off per capability where comparable (D16), scored against the profile's soft `budget_tokens` target when one is set (D20); currency only where prices are set (D19) |
| `latency` | median claim→finish seconds per cell | the same, per capability, with median turns shown beside it |
| `locality` | `row.local` | unchanged |

The weights are still the user's and still normalise to one. What a weight buys is unchanged; what it is weighing is now specific to the work.

### 7.2 Cost is tokens to a signed-off outcome, not tokens per attempt

For a cell, `tokens_to_signoff = Σ(tokens_in + tokens_out) over every finished attempt in the cell, bounced included, among attempts that reported / signed_off`, suppressed entirely below 80% reporting with the PRD-38 D11 sentence ("not comparable: 3 of 11 attempts reported tokens"). This is the existing proxy, now per capability, and the re-key is what makes it mean something: a cheap model at 12k tokens per attempt that signs off 30% of migrations costs 40k tokens per migration that lands, before the reviewer's tokens; a frontier model at 30k per attempt that signs off 90% costs 33k. Per attempt the cheap one wins; per outcome it does not, and the outcome is what anyone is paying for.

Onto the axis, when the profile sets no target: among the rows eligible for a request, those with a comparable number are rank-scaled so the cheapest reads 1.0 and the most expensive 0.2 (the span `cost_class` already uses); rows without a comparable number keep their class value. When the profile sets `budget_tokens`, the D20 curve replaces rank-scaling for every comparable row. The explanation carries both forms — `cost: measured 84k/sign-off (0.45)` and `cost: class cheap (0.60)` — so a mixed comparison is visibly mixed. Across vendors the proxy stays loose, because tokenizers differ; the D11 words carry over to every place the number appears.

### 7.3 Token utilization as a grade of its own

Beside the rate, each capability cell shows three utilization facts, each with its own reporting count: tokens per sign-off (or the reason it is not comparable), median turns used against the turn budget, and the share of attempts whose `exit_meaning` was budget exhaustion. A model that exhausts its budget on 40% of B4 query changes and 4% of E3 docs changes is telling you where its context runs out, and that is a fact about fit rather than about the budget. Rollups gain `turns_used`, `turns_reported` and `budget_hits` to carry it (§8). Review is costed separately: F-family cells carry the reviewer's tokens, and the page shows "cost of build" and "cost of review" side by side per capability, never summed, because the denominators differ.

### 7.4 Available harnesses: what the machine has is a fact, and its cost is measured

The resolution order is deliberate: policy, then profile, then installed, then score, so the explanation can say "claude:sonnet would have scored 0.9 on A4 and is not on this machine". Every launch post already records `dropped_rows` with the reason. R6 (D17) reads them: when the same capability keeps dropping a better-graded row as `not installed`, the card says how often, by how much, from which layer the grade came, and prints the install or serve line `doctor` prints. A person installs; the next launch resolves differently and the card retires on its hash. The same reading extends the existing cards per capability: a row repeatedly dropped by policy feeds R4 for that capability's lane, and one dropped by the profile's `defaults` feeds R3 under D18's family rule. On the grid an uninstalled or excluded row is greyed with its grade and its layer, not omitted — a blank cell would read as "unmeasured", which is a different claim from "measured elsewhere, unavailable here".

### 7.5 One ordered list, many capabilities

`defaults` does two jobs: membership (nothing outside it is spawned for this user) and tiebreak (after verified-over-unverified, before matrix `order`). It never did the scoring, and with quality per capability it should not start: a list cannot say "gbagent first for docs, claude first for migrations", and the score already does. So D18 keeps one list, makes the reorder card fire only on a family-level beat across two or more capabilities, and shows the per-capability picture on the grid instead. Per-family default lists were considered and rejected: they would be taste entered per function set, which is precisely the thing the measured cells exist to replace.

### 7.6 What a weight still cannot buy

- A row policy removed. `local_only` drops every cloud row before any weight is read, and the refusal names the policy (PRD-37 D4, unchanged).
- A score from a cell under the floor. Below `n = 5` a capability is `unmeasured` for that row at that layer and falls through to the next layer; the committed prior is the last resort and is labelled as one.
- A row over a project cap. `caps` filter before any weight is read (D20); a user's `budget_tokens` only bends the cost axis, and the two are reported as different drops.
- Money without prices. Tokens are a proxy for cost; they become currency only where a row carries a price, and a comparison never shows currency for one side and tokens for the other as if they were the same column.

### 7.7 Putting it together: the selection

The grading grid is not the chooser. The chooser is the resolver in `gbfleet`, and it reads three things: the grid (facts and measurements, by layer), the project's policy (constraints), and the user's profile (preferences). D21 fixes the order. A worked example, for a `cheap` request on an item whose touchpoints derive to `{A4 migration, B1 REST surface}`, size band `M`:

| Stage | Input | Effect | Recorded |
|---|---|---|---|
| rows | matrix rows for `cheap` | 6 rows | `matrix: 6` |
| policy · `allowed_harnesses` | `[gbagent, claude, qwen-code]` | drops `cursor-agent`, `codex` | `dropped.policy: cursor-agent (allowed_harnesses), codex (allowed_harnesses)` |
| policy · `local_only` | off | nothing | — |
| policy · `caps.per_item_tokens` | 120k, 70k already spent on this item | drops any row whose expected tokens-to-sign-off on `{A4, B1}` exceeds 50k: `claude:sonnet` at 84k measured | `dropped.policy: claude:sonnet (per_item_tokens: 84k expected > 50k left)` |
| profile · `defaults` | `[gbagent, qwen-code]` | both remain | `profile: alex (override for graphban)` |
| profile · `excludes` | `[gbagent:qwen3-coder:30b]` | drops that row | `dropped.profile: gbagent:qwen3-coder:30b (excludes)` |
| installed | gbagent adapter + served model; qwen 0.23.0 on PATH | both remain | `after_installed: 2` |
| score · quality | A4: gbagent 0.40 (project, n=7), qwen-code 0.78 (platform prior, n-band 50–199); B1: gbagent 0.85 (project, n=11), qwen-code 0.70 (org, n=6) | gbagent 0.63, qwen-code 0.74 | per capability, with layer and `n` |
| score · cost | budget target 50k; gbagent 31k measured; qwen-code class `cheap` (0 of 4 reported) | gbagent 1.0, qwen-code 0.60 (class) | `cost: measured 31k (1.0)` · `cost: class cheap (0.60)` |
| score · latency, locality | medians 214s vs 61s; local vs cloud | qwen-code leads latency, gbagent leads locality | values with `n` |
| weights | cost .27 · quality .41 · latency .09 · locality .23 | gbagent **0.79**, qwen-code 0.62 | both scores, the runner-up named |
| result | — | `gbagent:qwen3.6`, runner-up `qwen-code` | `source: matrix`, `profile: alex`, the full object above |

Read what the explanation lets a person see: the project's cap removed the row with the best A4 grade, the user's weights then preferred the local row despite its weaker migration grade, and the platform prior on qwen-code's A4 is labelled as a prior. Change the cap and the explanation changes; change the weights and it changes differently; neither is hidden inside a score. The same object goes to the launch post (PRD-38 D3), so `sampled` and the replay keep working, and R6 can see that nothing was dropped as not installed here.

What "enabled harnesses" means in this pipeline: the project's `allowed_harnesses` is the outer fence (a constraint an owner can be held to), the user's `defaults` is the inner one (taste, theirs to change), and installed is the machine's fact. All three are visible as separate drops, because "why did it not pick claude" has three different honest answers and the page should give the right one.

## 8. Data model

- `attempt_telemetry` gains `capabilities` (JSON list of §5 ids), `capabilities_at_delegate` (JSON list, from touchpoints only), `diff_shape` (JSON, nullable), `tool_errors` (int, nullable), `sampled` gains the value `probe`.
- `harness_rollups` and `platform_rollups` gain `capability` (text) in the unique key; `task_class` is dropped from new rows and backfilled into `capability` by the D3 mapping for existing rows in the same migration.
- `fleet_profiles` gains `budget_tokens` (int, nullable — the soft per-sign-off target, D20). `projects.fleet_policy` gains `caps` (`{per_attempt_tokens, per_item_tokens, per_period_tokens, period}`, all nullable). Per-item and per-period spend are sums of REPORTED tokens on `attempt_telemetry` for the item and the project in the period; the page shows the unreported share beside them.
- `harness_review_checks` (new): `id`, `delegation_id` (the review attempt), `reviewer_agent_id`, `verdict`, `checked_at`, `contradicted_by` (nullable event id), `kind` (`miss` | `false_bounce` | `confirmed`), `capabilities` (of the work reviewed). Written by a nightly pass over the 14-day window.
- `capability_priors` (new): `vendor`, `model`, `binary_version`, `capability`, `size_band`, `rate`, `n_band`, `source` (`platform`), `snapshot_at`. Replaced whole on each fetch.
- `capability_probe_runs` (new): `id`, `project_id` (the scratch project), `trigger` (`new_row` | `version_change`), `vendor`, `model`, `binary_version`, `item_ids` (JSON), `started_at`, `finished_at`, `summary` (JSON per capability).
- `platform_contributions` (new, hosted only): `instance_id`, `received_at`, `rows`, `snapshot_version_seen`.
- `harness_rollups` and `platform_rollups` also gain `turns_used`, `turns_reported`, `budget_hits` (§7.3). The platform snapshot carries `tokens_per_signoff` banded and `budget_hit_share` rounded, nothing per attempt.
- `matrix.toml`: `evidence` entries gain optional `capability`; rows gain optional `price_per_mtoken_in` and `price_per_mtoken_out` (D19).
- No new MCP tool. `fleet_status.measured` and `brief.measured_for_lane` re-key on capability; the brief's byte bound is re-asserted.

---

## 9. Acceptance criteria

1. A finished attempt whose touchpoints include `alembic/versions/…` and whose diff adds a route file derives to `{A2, A4, B1}` and contributes to all three cells; the rate in each is `signed_off / finished` over every attempt in the cell. Sabotage: derive a primary label only and the A2 cell empties.
2. An attempt that derives to no leaf lands in its family's `other` cell, which the page shows with its count; the derivation's coverage (`attempts with ≥ 1 leaf / attempts`) is a number on the page.
3. A leaf cell under `n = 5` is shown grey with its `n` beside its family rollup; the rollup is labelled as one. Sabotage: serve the rollup as the leaf and a test fails on the label.
4. `delegate` on an item with touchpoints under `web/src/features` records `capabilities_at_delegate = [B5]`; the brief carries it; the exit-time set may be larger and both are on the row.
5. The resolver's explanation for a `cheap` request names, per capability, the layer that supplied the quality cell (`project`, `org`, `platform`, `prior`) and the `n`; a request whose item has three capabilities of which one has only a platform prior says exactly that.
6. Under D6, a sign-off followed within 14 days by a `bug` item sharing ≥ 1 touchpoint writes a `miss` check keyed on the reviewer and the reviewed work's capabilities; a bounce followed by `suite_green` on the same head and a human `sign_off` writes `false_bounce`. Sabotage: widen the window to 15 days and a fixture fails.
7. F1–F3 cells appear for a reviewer only after five checked verdicts; below that they are grey with the count.
8. `GET /api/harness/probe/candidates` lists closed items with a red sabotage grouped by leaf capability; choosing one to three per leaf and running the panel against a vendor/model creates a `capability_probe_runs` row and delegations with `sampled = probe`; probe and natural rates for the same cell are shown as two numbers with two `n`s and never summed.
9. A matrix row first resolved with no cell for it, or a row whose `binary_version` changes, produces a probe suggestion on the page and in `doctor`; nothing runs without a person starting it.
10. R5 drafts an evidence line for a capability when a cell at `n ≥ 5` differs from the row's prior for that capability by ≥ 0.3; the card carries the replay, the siblings, the hash, and the probe label when probes contributed. Sabotage: hash only the proposal and the card cannot return when its cells move (the PRD-38 defect, retested).
11. The org view aggregates capability cells across the org's projects with a per-project breakdown; a card at org scope names the projects.
12. A self-hosted instance with `telemetry_share` on posts rollups nightly over the sync credential; the payload contains only the D11 fields (a test asserts the key set); the sync page shows the last contribution; turning the toggle off stops posting and the hosted recompute drops the contributor.
13. The hosted service serves a cell only under D13's three floors, unchanged; the snapshot carries `n` as a band and no identifier; an instance that fetches it resolves with `prior` cells labelled `platform` and dated, and a local cell at the floor outranks it.
14. `matrix.toml` evidence entries may carry `capability`; a row's status for a capability reads the newest entry naming it, else the row's status; `doctor` prints both.
15. The manifest is unchanged (`test_mcp_footprint`), and the brief's serialised bound holds with `capabilities` inside it.
16. Cost axis (D16): two eligible rows on a request, one comparable at 84k tokens per sign-off and one at 41k, rank to 0.2 and 1.0 and the explanation carries both numbers; a row under 80% reporting stays on its `cost_class` and reads `cost: class`. Sabotage: count only signed-off attempts' tokens in the numerator and a model that bounces most of its work reads as cheap — a fixture with a 30% signed-off row fails.
17. Utilization: a cell shows tokens per sign-off or its reason, median turns against budget, and budget-exhaustion share, each with its reporting count; build and review costs for one capability are two numbers and never one.
18. R6 (D17) fires with the drop count, both grades and their layers, and the remedy line, only when the dropped row's grade comes from a measured layer; a row known only from the committed prior draws no card. Sabotage: let the prior count and R6 fires for every unverified row that was ever dropped.
19. R3 under D18 drafts a reorder only when the top default is beaten at the family level across ≥ 2 capabilities; a single-capability beat is on the grid and drafts nothing.
20. Prices (D19): with prices on both rows the explanation shows expected spend per sign-off in currency; with a price on one row, currency appears for that row only and the axis ranks on tokens; with none, no currency appears anywhere.
21. Caps (D20): with `per_item_tokens = 120k` and 70k reported on the item, a row whose expected tokens-to-sign-off on the item's capabilities is 84k is dropped and the explanation names the cap and both numbers; a row whose vendor reported tokens on fewer than 80% of attempts is dropped under any cap with `tokens not reported`. Sabotage: let unreported rows through a cap and a fixture with a silent vendor passes as under budget — it must fail.
22. Budget target (D20): with `budget_tokens = 50k`, a row at 31k scores 1.0 on cost, one at 84k scores between 0.2 and 1.0 by the stated curve, and no row is removed; without a target the D16 rank-scaling applies.
23. Precedence: a number present in both `caps.per_attempt_tokens` and `budget_tokens` is applied as the cap first; the explanation records one policy drop and no profile effect for the row it removed.
24. The selection (D21): the launch post and the spawn reply carry, per stage, the rows dropped with reasons, and for the winner the four axis values each with its source and `n`; the §7.7 example, run as a fixture, produces that object byte-for-byte modulo timestamps.
25. A supervisor-side release that leaves the child's declared vendor, model and `binary_version` unchanged starts no new cell and suggests no probe; a declared change does both, and an ignored suggestion leaves the new cells filling from traffic with an inherited, labelled prior.
26. A `miss (unconfirmed)` check is written when a bug is filed on overlapping touchpoints, becomes `miss` when it closes fixed and `withdrawn` when it closes not-a-bug; F2 shows the two counts separately.
27. A probe cell under `n = 5` is grey, contributes to no D5 layer and fires no R5; a family-level panel produces a family-level probe cell under the same floor.
28. With every eligible row unreporting under a cap, the resolution is refused naming the cap, and an R6-shaped card lists the rows with their reporting shares; no row is scored on `cost_class` under that cap. Sabotage: fall back to `cost_class` and a silent vendor wins under a cap it never reported against.
29. A model string contributed by fewer than three instances is stored as `other` under its vendor and never served; the third instance's report un-redacts it forward only.
30. A withdrawn review check (bug closed not-a-bug or `unrelated`) recomputes the reviewer's F cells within the window; the F2 label reads "by touchpoint overlap".
31. A probe run transmits, if the instance contributes, exactly the D11 field set plus `sampled = probe` and nothing else — asserted by the same key-set test as criterion 12.
32. Operating loop, on the deployed instance: run the probe panel for gbagent:qwen3.6 at its current version across A1, A4, B3, E3 and C1; read the grid; confirm the E1 cell explains the PRD-38 walk's nine plumbing bounces as branchless attempts at the previous version and the docs cell reads 1/1 below the floor; confirm one R5 card fires or the page says exactly which threshold it missed. Recorded as `note` evidence.

---

## 10. Phasing

**PR 1 — the axis.** Derivation in `harness.py` with fixed-diff tests; `diff_shape` and `tool_errors` in the exit post; the migration re-keying rollups and backfilling `task_class`; the enum served on `GET /api/harness`; the Harness page grid with family rollups. Criteria 1–3, 15.

**PR 2 — the resolver and the record.** `capabilities_at_delegate` on the record and the brief; `measured` re-keyed; gbfleet scoring per D5 with the layered explanation, the D16 cost axis, the D20 budget target and caps, the D21 stage record; matrix prices (D19); `doctor`'s grid. Criteria 4, 5, 14, 16, 20–24, 25, 28.

**PR 3 — review competence, probes and utilization.** `harness_review_checks` and the nightly pass; F1–F3 cells; probe candidates, runs, and the two-number cells; suggestion triggers; the utilization facts on the grid (§7.3). Criteria 6–9, 17, 26, 27, 30.

**PR 4 — corrective and cross-user.** R5 and R6; R3 re-keyed to the family rule; org re-key; self-hosted contribution; the platform snapshot and `capability_priors`; the sync page. Criteria 10–13, 18, 19, 25–29, 31, then 32.

---

## 11. Risks and open questions

### Risks

- **Derivation drift.** Path heuristics rot as the tree moves. Mitigated by fixed-diff tests per leaf and the visible coverage number (criterion 2); a coverage drop is a page fact, not a silent reclassification.
- **Multi-label inflation.** One attempt counted in five cells can make a model look prolific. Mitigated by showing `attempts` and `cells` separately and by rates being per cell; no total ever sums across cells.
- **Probe contamination.** A model that has seen an item's diff in training would ace the probe. Mitigated by choosing panel items from the instance's own private history and by labelling probe rates separately; not solvable in general and said so.
- **Contribution as leakage.** A closed enum and week granularity carry very little, but `binary_version` plus a rare model string could identify an instance to itself. The floors already handle the "one huge org" case; the snapshot bands `n`. The claim stays "resists casual re-identification".
- **The prior anchoring the posterior.** A platform prior that is wrong for this repository could steer the resolver until five local attempts exist. Mitigated by D5's ordering and by every explanation naming `platform` when it applies, so an operator can turn the fetch off.

### Open questions

1. Should the reviewer's cells key on the work's capabilities (D6, proposed) or on the reviewer's own — i.e. is "misses migrations" or "over-bounces small changes" the more useful statement? (Not raised by the grill; open for the decompose.)
2. Is 14 days the right contradiction window for F2, or should it be "until the next release stamp"? (The grill settled what happens INSIDE the window — D6 — not its length.)
3. Should a probe run consume the operator's normal seats and keys, or a dedicated probe credential the ledger can recognise without the `sampled` label?
4. Does self-hosted contribution need a separate consent from `telemetry_share`, given that it leaves the instance rather than the org?
5. ~~Should a hard token budget be a preference or a constraint?~~ Resolved before the grill: both, as D20 — a cap on the policy filters, a target on the profile scores, the cap wins where both name a number. The grill should test the curve above the target and the `tokens not reported` rule.
6. Should prices live in the matrix (committed, one price per row) or on the project's policy (a hosted org's contract pricing differs from list price)? D19 says matrix; the grill should test that.
7. Should the snapshot be signed, so an instance can verify it came from the hosted service, or is TLS over the sync credential enough for a prior that a local cell can always override?

---

## 12. Prior art

- PRD-37 — the matrix as facts, policy as filter, profile as weights; `measured` cells; the explained resolution.
- PRD-38 — the attempt record, rollups, floor and skew, R1–R4 as drafts, the replay, org scope and the platform overlay with its three floors; the walk whose 0/5 cell motivates §2.2.
- PRD-39 — the fleet reviews itself, which is why F is a family.
- PRD-16 — lessons as candidates a human publishes; the same posture for R5.
- The Build → Harnesses design (`graphban-local`, `pages/build-harnesses-proposed`) — where the grid, the probes and the cards land.
