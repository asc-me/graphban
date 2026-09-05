# PRD-37 — Preference matrix: committed harness/model facts, project policy, user weights, supervisor-side resolution

**Ledger id:** GRPH-P37
**Status:** draft — v0.1, not yet grilled.
**Depends on:** PRD-36 (tier table, `spawn(tier)`, bound seats) · PRD-35 (the delegation record and its deferred outcome statistics) · PRD-22 (adapters, `Support`, `doctor`) · PRD-24 (gbagent, the qwen finding)
**Complemented by:** the Fleet view (edits profiles and policy) · `gbfleet doctor` (prints the matrix against the machine) · `harness-kit` review of 2026-09-04 (the routing-table idea, kept; its hook enforcement, not)
**Touches:** `fleet/src/gbfleet/matrix.toml` (new; the PRD first said `fleet/matrix.yaml` — TOML because gbfleet is httpx-thin, PRD-22 D-e, and `tomllib` is stdlib; package data so an installed wheel carries it) · `fleet/src/gbfleet/matrix.py` (new) · `fleet/src/gbfleet/{mcp,until,doctor,cli}.py` · `fleet/src/gbfleet/adapters/qwen_code.py` (new) and `adapters/__init__.py` · `backend/app/models/__init__.py` (`FleetProfile` new, `Project.fleet_policy`) · `backend/alembic/versions/` · `backend/app/services/fleet.py` · `backend/app/routers/fleet.py` · `backend/app/mcp_server.py` (result payloads only) · `web/src/features/fleet/` · `docs/fleet-adapters.md` · `docs/mcp.md`

---

## 1. Overview

<!-- framing -->

PRD-36 gave a parent two calls to hand an item to a cheaper model: `delegate(seat=true)` and `spawn(tier=…)`. It left the meaning of a tier to a flag the operator types at each launch, `--tier cheap=gbagent:qwen3.6:35b-a3b-coding-mtp-det`, and it left "which harness and model should run this kind of work" as knowledge in one person's head. Three things now exist that could answer that question and do not talk to each other: each adapter's support record (which harness versions were verified), the per-machine tier flags (what runs), and the delegations table (what happened when it ran).

This PRD joins them into three layers, each owned by the party that can know it:

1. **Facts**, a committed matrix of harness, model, lane, role and tier rows with a status of `verified`, `unverified` or `failed` and the evidence for it. Reviewed like code.
2. **Policy**, hard constraints a project sets: no code to a cloud model, a reviewer must be cross-vendor. Stored on the server, enforced as a filter, never a weight.
3. **Preferences**, a profile each user sets: the harnesses they want used by default, in order, and weights over a few axes. Stored on the server so the Fleet view edits them and the brief shows them.

The supervisor resolves. When a delegation asks for a tier, `gbfleet` takes the matrix rows the project's policy allows, keeps the harnesses the user listed and the machine has installed, scores them by the user's weights, and spawns the top one. It then says, in the spawn reply and its log, which rows were eligible, which won, and the numbers that decided it.

The load-bearing invariant, inherited from PRD-35 and PRD-36:

**Facts are committed, policy filters, preferences score, the supervisor resolves and explains. The server stores and shows all three; it never picks a harness.**

### 1.1 What this is not

- **Not a model router in the server.** The server knows nothing about what is installed on the machine that will spawn, so it cannot resolve. It records what the delegation asked for and what turned up.
- **Not a learned scheduler.** Measured axes come from the ledger with their sample counts and feed the same scorer; nothing trains, nothing adapts on its own. A row with too few attempts scores as unverified, not as good or bad.
- **Not a replacement for `--tier`.** The flags stay as the explicit override. The matrix and profile are the default source when no flag is given.
- **Not a benchmark.** `verified` means a spawn walk on a real item was signed off, with the item named. It does not rank models.

---

## 2. Problem

<!-- framing -->

Verified against the tree at `f34ed5be` and the deployed check of 2026-09-05.

### 2.1 Fitness knowledge lives in one head and one memory file

That `qwen3.6:35b-a3b-coding-mtp-det` builds and gets signed off while `qwen3-coder:30b` fails every build is recorded in PRD-24's acceptance walk and in an operator's notes. Nothing in the repo or the ledger states it as a fact a supervisor can read. A second operator on a second machine starts from nothing.

### 2.2 The adapter registry and the adapter directory disagree

`ADAPTERS` holds `claude`, `cursor-agent`, `gbagent` and `grok`. `adapters/codex.py` exists and is not registered. `doctor` prints a support matrix of harness versions and cannot say "codex: present, unregistered" or "qwen-code: no adapter", because the matrix is derived from the registry. Absence reads as clean.

### 2.3 A tier is whatever the last launch said

`--tier cheap=…` is typed per launch, per machine, fixed for the process (PRD-36 D16). Two supervisors give `cheap` two meanings and nothing records either beyond the spawn reply. A user who always wants gbagent first and claude only for review has nowhere to say so once.

### 2.4 The measured evidence is written and never read

`delegations` carries requested tier, declared model and outcome per attempt (PRD-35 D9, D10), and the supervisor records registration latency per child. PRD-35 deferred statistics until there was volume. There is now some, and nothing reads it.

### 2.5 The manifest has eight tokens of headroom

Nothing in this PRD may add a Graphban tool or a schema property. Profiles and policy travel in result payloads of calls that already exist, and through REST for the Fleet view.

---

## 3. Goals

1. One committed file says which harness and model pairs are verified for which lane, role and tier, with the evidence, and `doctor` prints it beside what the machine actually has.
2. A user states once which harnesses they want used by default and how they weigh cost, quality, latency and locality; a project states its hard constraints once.
3. `spawn(tier=…)` with no `--tier` flag resolves through matrix, policy, profile and installed adapters, and its reply explains the choice.
4. Latency and quality are measured from the ledger and carry their sample count; small samples score as unverified.
5. Qwen Code has an adapter, walked on a real item before its row reads `verified`.
6. No new Graphban tool, no manifest ceiling raise, no server-side choice.

## 4. Non-Goals

- Choosing which items to delegate, or what tier to ask for. The brief suggests, the parent decides (PRD-35 D5).
- Per-item or per-call weights. A profile is per user, optionally per project; a delegation carries a tier, not a scorer.
- Automatic status changes in the matrix. A row moves to `verified` or `failed` by a commit that names the item, never by a job.
- A UI for editing the matrix. It is a file; the Fleet view edits profiles and policy only.
- Ranking vendors against each other in prose or docs.
- Adapters for Codex, Gemini CLI or anything beyond Qwen Code. Codex's unregistered file gets a matrix row saying so and nothing else.

---

## 5. Key decisions

| # | Decision | Why |
| --- | --- | --- |
| D1 | Three layers, three owners: facts in the repo, policy on the project, preferences on the user | A fact needs a reviewer; a constraint needs an owner who can be held to it; a weight is taste and is the user's to change without asking anyone. Mixing them puts taste into a fact table or facts into a settings page. |
| D2 | The matrix is `fleet/src/gbfleet/matrix.toml` (TOML, package data; written as `fleet/matrix.yaml` before the no-yaml-dependency constraint was checked), one row per harness × model × lane × role × tier | Columns: `harness`, `model`, `lane` (frontend/backend/mixed/any), `role` (worker/reviewer), `tier` (cheap/frontier), `status` (verified/unverified/failed), `evidence` (item id and date for verified or failed), `order` (preference within a tier), `cost_class` (local/cheap/frontier), `local` (bool). A row whose harness is not in `ADAPTERS` is `status: unregistered`, and doctor says so. |
| D3 | A profile is `defaults` and `weights`, plus optional `excludes` | `defaults`: an ordered list of harness names the user wants considered; nothing outside it is spawned for that user. `weights`: `cost`, `quality`, `latency`, `locality`, each 0–1. `excludes`: harness or model ids never to use. One profile per user, optionally overridden per project. |
| D4 | Policy is a project's hard constraints, applied as a filter before any scoring | `local_only` (no cloud harness or model), `reviewer_cross_vendor` (a reviewer row's harness or model vendor differs from the builder's), `allowed_harnesses`. A constraint removes rows; it never adjusts a score, so a strong preference cannot outvote it. |
| D5 | The supervisor resolves at spawn, in this order: matrix rows for the tier and role → policy filter → profile defaults and excludes → installed adapters and served models → score → matrix order | Installed is checked last only because it is the cheapest to explain: "gbagent won on score but is not on this machine" is a message worth giving. A resolution with no eligible row is refused naming the emptying step. |
| D6 | Score is a weighted sum over four axes, each 0–1 | `cost` from `cost_class` (local 1.0, cheap 0.6, frontier 0.2); `locality` from `local`; `latency` and `quality` measured (D7). A user's weights are normalised to sum to one. Tie: matrix `order`. |
| D7 | Measured axes carry their sample size, and small samples do not score | `latency` from the supervisor's `registration_latency` and the ledger's turn durations per harness and model; `quality` from `delegations` as signed-off over finished attempts per harness, model and lane. Fewer than five finished attempts: the axis contributes nothing and the explanation says `n=<k>, unmeasured`. PRD-35 named the bias — frontier only sees what cheap failed — so quality is shown per lane and per tier requested, never pooled. |
| D8 | Every resolution explains itself | The spawn reply gains `resolution`: the eligible rows after each filter (counts and the names dropped), the winner, its axis values and score, the runner-up, and the profile that decided it. The same object goes to the gbfleet log and to `until`'s report. A weighted choice nobody can read is the hook pack's failure mode. |
| D9 | The server stores and serves profiles and policy through what already exists | REST for the Fleet view; `fleet_status` and `get_item_details.brief` carry the caller's profile summary and the project's policy in their result payloads. No new tool, no schema property. The supervisor reads them through `fleet_status`, a call it already makes on every tick. |
| D10 | `--tier` stays and overrides | Given, it is the resolution and the reply says `source: flag`. Absent, matrix and profile resolve and the reply says `source: matrix`. The PRD-36 D16 rule holds either way: the table is fixed for the life of the process; a profile change is read at the next launch, not mid-run. |
| D11 | `doctor` prints the matrix against the machine | For each row: status, evidence, installed (adapter resolves and the model is served), measured latency and quality with `n`. Then, for each tier, what this machine would resolve to for the operator's profile, with the explanation. An unregistered adapter file is a line, not a silence. |
| D12 | Reviewer rows resolve like worker rows, under `reviewer_cross_vendor` | The builder's declared vendor and model are on the delegation record; a reviewer resolution filters rows whose vendor matches when the project says so. This is the tier-aware review pairing PRD-35 deferred, done as a policy filter rather than a rule in `claim_review`. |
| D13 | Qwen Code is the first new adapter, and its row starts `unverified` (landed 2026-09-05 against 0.23.0 — see `docs/fleet-adapters.md` for what was measured: `httpUrl` only, `--allowed-mcp-server-names`, an unenforced `-m`) | Same shape as the four registered adapters: launch argv, where the seat's MCP config is read from, a version range with a build actually run, exit-code meanings, debug flag or its absence. Its row moves to `verified` by the commit that names the item the spawn walk signed off. |
| D14 | The profile that resolves is the credential's owner's | The supervisor runs under an API key; the key has a user; that user's profile applies. A key with no user resolves with the project's policy and the matrix order alone, and the explanation says `profile: none`. |

## D5 — Resolution

```
rows      = matrix[tier, role, lane or any]
rows      = policy.filter(rows)                    # local_only, allowed_harnesses, reviewer_cross_vendor
rows      = [r for r in rows if r.harness in profile.defaults and r not in profile.excludes]
rows      = [r for r in rows if installed(r.harness) and served(r.model)]
scored    = [(score(r, profile.weights, measured), r) for r in rows if r.status != "failed"]
winner    = max(scored, key=(score, -order))       # order breaks ties
```

Each step's survivors and casualties are kept for the explanation. `failed` rows are never spawned; `unverified` rows are eligible, and the explanation marks the winner as unverified when it is, because a user who listed only unverified harnesses has asked for exactly that.

## D8 — The explanation

```
resolution: {
  source: "matrix" | "flag",
  tier, role, lane,
  eligible: {matrix: 6, after_policy: 4, after_profile: 3, after_installed: 2,
             dropped: {policy: ["claude:opus (local_only)"], profile: ["grok"], installed: ["cursor-agent (not on PATH)"]}},
  winner: {harness, model, status, score, axes: {cost: 1.0, quality: {value: 0.75, n: 4, used: false}, latency: {...}, locality: 1.0}},
  runner_up: {...} | null,
  profile: {user, defaults, weights} | "none",
}
```

---

## 6. Data model

`fleet_profiles` (new, alembic next): `id`, `user_id` fk users, `project_id` fk projects nullable, `defaults` JSON list, `weights` JSON, `excludes` JSON list, `updated_at`. One row per user per project, one with a null project as the default.

`projects` gains `fleet_policy` JSON nullable: `{local_only, reviewer_cross_vendor, allowed_harnesses}`. Null is no constraint.

`fleet/matrix.yaml` (new, committed). `gbfleet` gains `matrix.py` (load, filter, score, explain) and reads `fleet_status` for profile and policy. `doctor` gains the matrix section. `adapters/qwen_code.py` (new) and a registry entry.

No new MCP tool. `fleet_status` and `get_item_details.brief` results gain `profile` and `policy` keys; no outputSchema change.

---

## 7. Acceptance criteria

Each is a test. Sabotage the call, not the model.

1. `fleet/matrix.yaml` loads; every row's `harness` is either in `ADAPTERS` or has `status: unregistered`; a row with `status: verified` or `failed` carries an item id in `evidence`. The test fails on a verified row with no evidence.
2. A harness file present in `adapters/` and absent from the registry (codex today) is a matrix row with `status: unregistered`, and `doctor` prints it. Sabotage: register nothing and delete the row; the test that lists adapter files fails.
3. A profile is stored per user and per user-project; the Fleet view reads and writes it over REST; a project's policy is stored and read the same way.
4. `fleet_status` carries the caller's profile summary and the project's policy; `get_item_details.brief` carries the same. A key with no user carries `profile: null`.
5. Resolution order is as D5: a `local_only` project with a profile that weights quality 1.0 still never resolves to a cloud row. Sabotage: score before filtering and this fails.
6. A profile whose `defaults` omit a harness never resolves to it, even when that harness is the only installed one; the refusal names the profile as the emptying step.
7. A row not installed on the machine is dropped after scoring and the explanation names it and the reason.
8. Weights normalise: `{cost: 2, quality: 2}` resolves identically to `{cost: 0.5, quality: 0.5}`.
9. Ties fall to matrix `order`.
10. A `failed` row is never spawned; an `unverified` winner is marked unverified in the explanation.
11. Measured quality with `n < 5` contributes nothing and the explanation says `unmeasured`; with `n ≥ 5` it contributes signed-off over finished. Sabotage: pool lanes together and the per-lane test fails.
12. `spawn(tier=…)` with no `--tier` flag resolves through the matrix and the reply carries `resolution` with `source: matrix`; with the flag, `source: flag` and the flag's adapter runs.
13. `until` resolves per delegation through the same function and its report carries each child's resolution.
14. A reviewer resolution under `reviewer_cross_vendor` drops rows whose vendor matches the builder's declared vendor; without the policy it does not.
15. `doctor` prints every matrix row with status, evidence, installed, and measured axes with `n`, then the resolution for each tier under the operator's profile.
16. The Qwen Code adapter resolves its binary, refuses out-of-range versions, launches with the seat's config where that harness reads it, and maps its exit codes; its matrix row is `unverified` until the walk.
17. Operating loop: on the deployed instance, set a profile with `defaults: [gbagent, claude]` weighted for cost, a project policy of `local_only`, run `spawn(tier="cheap")` with no flag, and read the reply: gbagent wins, claude is listed as dropped by policy, the explanation carries the numbers. Then flip the policy off and weights to quality with `n` below five: gbagent still wins on cost and locality with quality marked unmeasured. Recorded as `note` evidence.

---

## 8. Phasing

**PR 1, the facts and the resolver.** `fleet/matrix.yaml`, `gbfleet/matrix.py` with filter, score and explain, `doctor`'s matrix section, `spawn` and `until` resolving from the matrix with `--tier` as override. No server change; profile is `none` and policy is empty. Criteria 1, 2, 5–13 (with no profile), 15.

**PR 2, policy and preferences.** `fleet_profiles`, `projects.fleet_policy`, REST and Fleet view editing, `fleet_status` and `brief` carrying them, the resolver reading them. Criteria 3, 4, 6, 14, and 12–13 with a profile.

**PR 3, measured axes and Qwen Code.** Latency and quality from the ledger with `n`, the Qwen Code adapter and its spawn walk. Criteria 11, 16, 17.

---

## 9. Risks and open questions

### Risks

- **Weights are opaque.** Mitigated by D8: no resolution without its explanation, in the reply, the log and the report. If the explanation is ever dropped to save output, the weights should go with it.
- **Quality is a small, biased sample.** D7 carries `n`, refuses to score below five, and never pools lanes or tiers. The number will still be read as a ranking by someone; the doctor output says what it is beside every value.
- **The matrix drifts from the machine.** A verified row for a harness version no longer installed reads as verified forever. `doctor` prints installed beside status; the operating-loop check reads it. A stale row is visible, not silent.
- **Two profiles for one supervisor.** The supervisor runs under one key and applies its owner's profile, while the parent who delegated may be another user. D14 says whose profile applies and the explanation names them. Whether a delegation should carry the delegator's profile instead is open question 2.

### Open questions

1. Should `defaults` be an allowlist, as D3 says, or an ordering with everything else allowed at a penalty? An allowlist is easier to explain and matches "the user specifies what harnesses they want defaulted"; an ordering never leaves a user with an empty resolution.
2. Whose profile resolves a delegated item: the supervisor's key owner, or the delegator recorded on the delegation? The supervisor owns the machine; the delegator owns the request.
3. Should `failed` rows expire? A model that failed in July may build in September after an upgrade. A dated failure plus a manual re-walk is the current answer.
4. Is `locality` an axis or only a constraint? Some users want local-first without forbidding cloud; others forbid it. Both are offered; the grill should ask whether that is one knob too many.

---

## 10. Prior art

- PRD-36 D6 and D16: the per-machine tier table this generalises, and the immutability rule it keeps.
- PRD-35 D5, D7–D9: the brief's suggestion with a basis, the delegation record, and the deferred outcome statistics this PRD reads with their sample counts.
- `adapters/__init__.py` `Support`: the per-harness verified build the matrix extends to models and lanes.
- `harness-kit/claude-code/delegation` `routing.yaml` (reviewed 2026-09-04): a task-to-model table with a per-machine execution column; its idea of a committed routing table is kept, its per-call regex enforcement is not.
