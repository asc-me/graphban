# Rule candidates: hard-coded development policies (GRPH-1406)

A review of the constants, defaults and instructions in this repository that encode a
development choice, asked for on 2026-11-07 after `build.test_scope` (GRPH-1405) moved
"where the suites run" out of `AGENTS.md` and onto the Rules page. Line numbers are against
`main` at `c44479fb`.

## The line

A candidate is a **team's development choice** when two reasonable teams would set it
differently and both would still be using Graphban correctly. An **invariant** is something
whose opposite setting would make a Graphban receipt say something false: a sign-off that
attests nothing, an approval nobody earned, a credential that outlives what it was minted
for. Invariants stay hard-coded, and each one below says why.

Two pressures pull the other way, and each candidate has to survive both:

- **Config is the expensive one** (`AGENTS.md` "Simplest thing"). Every rule is a permanent
  branch in every deployment's behaviour. A candidate earns a rule only if the difference
  between teams is real now, not imagined. "Someone might want to tune this" is not enough.
- **A rule must not loosen an invariant by the back door.** Where a constant mixes the two
  (a floor that protects a receipt, plus a level a team might raise), the proposal makes
  only the team half configurable and keeps the floor.

The review found a **third class** the brief did not name: **this repository's own layout,
hard-coded into the product.** Paths like `backend/alembic/versions`, `web/` and
`gen_subagents.py`'s outputs are facts about Graphban's own repo. They are not team choices
at all. They are simply wrong, or inert, for every other project Graphban tracks. These are
the highest-priority entries, because a project that is not this repo gets the wrong behaviour
today with no way to fix it.

A fourth line separates **project rules** from **machine and run settings**. A setting that
describes the box a supervisor runs on (its memory, its CPU, how slow a vendor's CLI is to
start there) belongs on that machine or that run, not on the project. One project is drained
from several machines.

## Already configurable (do not propose again)

| Policy | Where a team sets it today |
| --- | --- |
| Where the suites run | `build.test_scope` (GRPH-1405) |
| Migration items need Postgres evidence | `build.engine_evidence` (library, default on) |
| Sabotage on every item, not only effort ≥ 3 | `review.sabotage_required`, param `min_effort` (library, default off) |
| Reviewer must be a different vendor | `review.distinct_vendor` (library, default off) |
| A noted gap blocks sign-off | `review.gap_blocks_sign_off` (library, default on) |
| Red CI reopens signed-off work | `merge.reopen_on_red` (library, default on) |
| Staff every visible review row | `review.staff_every_row` (library, default off) |
| Human-only clauses block | `accept.human_clause_blocks` (library, default on) |
| Reviewer bar (sign-off, forge, both) | `gitops.reviewer_bar` |
| Base branch, branch and PR title patterns, gitops model | `gitops.*` |
| `scans_clean` on merges into the release ref | the project's release ref (GRPH-P57) |
| Spend per worker | `plan.budget_cap` (org) |
| Claim lease length | `lease_seconds` on each claim (`backend/app/mcp_server.py:589`) |

## Candidates

Priority: **high** means wrong behaviour today for a project that is not this repo, or a gate
that has cost real waves. **Medium** means a real team difference with a workaround today.
**Low** means it is worth a rule only if someone asks.

### 1. Exclusive file areas — high

- **Location:** `backend/app/services/fleet.py:4248` (`HARD_AREAS = ("backend/alembic/versions",)`);
  the merge-time heads check `fleet/src/gbfleet/propose.py:819` (`merged_alembic_heads`).
- **Today:** overlapping leases are soft (both agents proceed and the collision is reported)
  except over `backend/alembic/versions`, which is exclusive, because two concurrent
  migrations leave two alembic heads (GRPH-1389).
- **Why it is a team choice, not an invariant:** *which* paths cannot be built in parallel is
  a fact about a project's stack. Another project's migrations live in `migrations/`,
  `prisma/migrations/` or `db/`, and it may have other single-writer files (a lockfile, a
  generated client, a schema dump). Today those projects get soft leases on exactly the files
  that need hard ones, and an exclusive lease on a path that does not exist. The *mechanism*
  (a hard area is exclusive) stays hard-coded. The *list* is the project's.
- **Proposed rule:** `plan.exclusive_areas`, stage Plan, param `paths` (list of globs).
  Default `["backend/alembic/versions"]`, so nothing changes on upgrade.
- **Related defect found here:** the merge-time heads check reads the same path, hard-coded
  as `_VERSIONS` (`fleet/src/gbfleet/propose.py:799`). In a repo with no such directory it
  finds zero heads and records `alembic-heads=1`, so a check that looked at nothing reads as
  a pass. A project whose alembic tree lives elsewhere gets no check. GRPH-1423 includes
  reporting `alembic-heads=none` when the directory is absent, so that case reads as
  unchecked rather than clean.
- **Filed:** GRPH-1423.

### 2. Generated files — high

- **Location:** `fleet/src/gbfleet/supervisor.py:1844` (`GENERATED_EXACT`, `GENERATED_PREFIXES`,
  `is_generated`).
- **Today:** files this repo's generators rewrite (`AGENTS.md`, `.claude/agents/`,
  `.cursor/agents/`, `.agents/skills/`, `backend/tests/shard_weights.json`, …) are exempt from
  the undeclared-file hold, with a `generated_files_allowed` warning (GRPH-1352/1375).
- **Why it is a team choice:** the list is `scripts/gen_subagents.py`'s output set. It is
  Graphban's own codegen, hard-coded in the supervisor every project runs. Another project's
  generated files (an OpenAPI client, `schema.graphql`, a lockfile, protobuf stubs) are held
  back from a PR as undeclared, and this repo's paths are exempted in projects where a person
  wrote them by hand.
- **Proposed rule:** `build.generated_files`, stage Build, param `paths` (list of globs, exact
  paths or prefixes). Default: today's two tuples, so nothing changes on upgrade. Level
  `checked`: only the supervisor honours it, like `review.staff_every_row`.
- **Filed:** GRPH-1424.

### 3. Undeclared files: hold or warn — high

- **Location:** `fleet/src/gbfleet/supervisor.py:1832` (`proposal_held_undeclared`).
- **Today:** a branch that changes a file outside every declared touchpoint is held back from
  a PR, with a wait naming the files. A planner must widen the touchpoints and the item waits.
- **Why it is a team choice:** how tightly declarations must predict a diff is a trade
  between collision safety and throughput. A team running one worker at a time loses nothing
  by warning. A team running ten wants the hold. This gate has stalled waves here when the
  touchpoints were simply incomplete. The **invariant** is a different check that stays:
  at merge, every changed file must sit inside a touchpoint of an item signed off at that
  commit (`propose.merge`, `covering`, GRPH-1408). Unreviewed files never merge under someone
  else's sign-off. This rule only moves *when* that is caught (at the PR, or at the merge).
- **Proposed rule:** `build.undeclared_files`, stage Build, param `mode`: `hold` (today) or
  `warn` (open the PR, record the files as a wait and on the item, leave the merge-time check
  to refuse). Default `hold`.
- **Filed:** GRPH-1425.

### 4. Predicates that `done` requires — high

- **Location:** `backend/app/config.py:381` (`required_predicates`, a deployment env setting);
  the `suite_green` requirement for an item with a linked PR,
  `backend/app/services/items.py:1338` (`refuse_if_pr_cooling_down`).
- **Today:** the set of attested predicates an item needs before `done` is one environment
  variable for the whole deployment. Every project on a box gets the same bar, and changing it
  is a redeploy by an operator, with no reason or history recorded. A project with no CI can
  never satisfy `suite_green` once it links a PR.
- **Why it is a team choice:** how strict the completion gate is ("how strict a gate is"
  in the brief) differs by project: a docs site, a prototype and a payments service on the same
  box want different bars. The **invariant** that stays: a predicate a project requires must
  be *attested* by something holding the `gate` scope, never self-reported, and a required
  predicate that did not run is not a pass (`review.predicates_compared`).
- **Proposed rule:** `merge.done_predicates`, stage Merge, param `predicates` (list of
  predicate names). Default: the deployment's `required_predicates`, plus `suite_green` for an
  item with a linked PR, which is today's behaviour. A person may add to it. Removing
  `suite_green` is the open question already filed as **GRPH-1059** ("should sign_off's
  suite_green requirement be waivable for a project with no CI?"). This rule is the shape
  that decision would land in, so the filed item depends on GRPH-1059 being answered for that
  one predicate and can ship the additive half without it.
- **Filed:** GRPH-1426.

### 5. Merge method — medium

- **Location:** `fleet/src/gbfleet/propose.py:1259` (`gh pr merge --squash --auto`) and
  `fleet/src/gbfleet/propose.py:880` (`gh pr update-branch --rebase`).
- **Today:** the supervisor always squashes, and always rebases a branch that is behind.
- **Why it is a team choice:** squash, merge commit or rebase is a standing team convention,
  and some forges' branch protection forbids one of them. A project that requires merge
  commits cannot use `--merge` at all today. The invariant is untouched: whatever the method,
  the PR's head must be the reviewed commit (`review.attest_the_head`).
- **Proposed rule:** a `gitops.merge_method` field beside the existing `gitops.*` rules,
  `squash` | `merge` | `rebase`, default `squash`. It is a gitops field rather than a library
  rule because the gitops view already carries the base-branch and PR conventions this sits
  with.

### 6. Child time limits — medium

- **Location:** `fleet/src/gbfleet/supervisor.py:78` (`child_wall_clock = 3600`), `:96`
  (`disowned_after = 1800`), `:103` (`quiet_after = 300`). Each is a CLI flag per run.
- **Today:** how long a child may run, and how long it may go quiet, is set per `gbfleet`
  invocation, with these defaults.
- **Why it is partly a team choice:** "how long an agent may take" depends on the project's
  test suite and build, not only on the machine. A project whose suite takes 25 minutes needs a
  longer wall clock on every machine that drains it, and today each operator must remember the
  flag. The machine half (how slow the box is) stays a flag, and a flag still wins.
- **Proposed rule:** `plan.child_time_limits`, params `wall_clock_s`, `quiet_after_s`,
  `disowned_after_s`. Defaults are today's numbers, read by the supervisor at launch when no
  flag is passed. Medium, because `--child-wall-clock` is a working override today.

### 7. Lane and task-class paths — medium

- **Location:** `backend/app/services/delegation.py:98` (`_is_web`: `web/` is frontend) and
  `:117` (`checklist_for`: `alembic/versions` or `models/__init__` is a migration,
  `mcp_server.py` is an MCP tool).
- **Today:** the delegation brief's lane and checklist suggestions read this repo's layout.
- **Why it is a team choice (repo-fact class):** another project's frontend is `app/`,
  `client/` or `packages/ui/`, so its frontend items are suggested as `backend`. These are
  *suggestions* with a `basis`, and the caller still types the lane, so nothing is refused.
  That is why this is medium, not high.
- **Proposed rule:** `plan.path_classes`, params `frontend` (globs, default `["web/**"]`) and
  `checklists` (a map from checklist name to globs, default today's). The checklist *names*
  stay tied to the task classes in the project's own `AGENTS.md`.

### 8. Local engines for agent-run suites — medium

- **Location:** `fleet/src/gbfleet/propose.py:566` (`LOCAL_ENGINES = ("postgres", "sqlite")`).
- **Today:** under `build.test_scope` `agent_full`, a merge with no CI attestation counts the
  builder's full local runs on exactly these two engines.
- **Why it is a team choice:** the engines are this repo's matrix. A project on MySQL or one
  engine cannot meet `agent_full` at all.
- **Proposed rule:** not a new rule. Add a param `engines` to `build.test_scope`, default
  `["postgres", "sqlite"]`. It only applies in `agent_full`.

### 9. Memory write mode on the Rules page — low

- **Location:** `backend/app/models/__init__.py:210` (`Project.memory_write_mode`).
- **Today:** already a per-project setting (`review`, `auto` or `trusted`), changed through
  the project settings update, which takes no reason. A rule toggle records who changed it and
  why.
- **Proposed rule:** surface it as `memory.write_mode` (`review` | `auto` | `trusted`,
  default the stored value), backed by the same column. No behaviour change. It is low because it is
  already configurable. The gain is that a change is recorded with a reason.

### 10. Reach vocabulary — low

- **Location:** `backend/app/services/reach.py:54` (`_PLATFORMS`) and `:192` (`_RULES`).
- **Today:** a fixed vocabulary of hosting platforms and verbs raises reach *signals*, which
  need `acknowledge_reach=true` to delegate.
- **Why only low:** the module states that signals are evidence for a person, never a verdict,
  and the one hard rule rests on the `reach` declaration. A project could add its own
  platform names (an internal deploy CLI) to raise more signals. Removing names would only
  hide signals, so any rule here would be add-only. This is low until a project reports a miss.

## Stays hard-coded

| Constant or gate | Location | Why it stays |
| --- | --- | --- |
| Not your own work | `backend/app/services/fleet.py` `SelfReview` | Review is a second opinion. Off, a sign-off attests nothing. |
| Attest the head, not the base | `fleet.py` `AttestedTheBase` | Off, a receipt names code nobody reviewed. |
| A check that could not run is not a pass | `fleet.py` `UncomparedPredicate` | The absence rule. Off, "nobody looked" reads as clean. |
| Every acceptance clause is tested | `fleet.py:2332` `MissingAcceptanceCoverage` | A sign-off is a claim about the acceptance. A team chooses how acceptance is *written*, not whether it is tested. |
| Approval is earned by the grill | `gate.approval_earned_by_grill` | An authority gate. A person's decision, never a setting. |
| `reach = deploy` is never delegated | `backend/app/services/delegation.py:839` | It rests on a declaration only a signed-in person can set. Work on a running system cannot be contained by a worktree. |
| Design prompts and reviews need a person's acceptance | `backend/app/services/documents.py:33` `ACCEPT_GATED` | An authority gate over governing documents. |
| Unreviewed files do not merge under a sign-off | `propose.merge` `covering` (GRPH-1408) | The invariant half of candidate 3. |
| A person adopts a rule | `rules.person_adopts` | D10: nothing changes rule state except a person's act. |
| Sabotage floor at effort ≥ 3 | `fleet.py:2237` `ADVERSARIAL_EFFORT_THRESHOLD` | Raising strictness is already a rule (`review.sabotage_required`). Lowering the floor would remove adversarial review from real work. The comment records why the threshold exists: a gate that fires on trivia gets routed around. |
| Quarantine after 3 refusals | `fleet.py:1717` | The code says "not a setting" on purpose: a drifting agent blocks the divvy for every project's agents, not one team's. |
| Fleet keys expire in a day | `fleet.py:4501` `FLEET_KEY_DAYS` | Credential lifetime. A credential that outlives its wave is one nobody remembers issuing. `gban setup` already mints a non-expiring project key for the case that needs one. |
| Enrolment codes live 30 minutes | `fleet.py:180` | Credential lifetime, same class. |
| Presence TTL | `fleet.py:205` `presence_ttl_seconds` | Derived from the lease. It is a measurement of liveness, not a policy. |
| Grill stall after 3 answers | `backend/app/services/prds.py:758` | How the grill reports its own failure to converge. A product behaviour, not team policy. |
| PR cooldown exists | `refuse_if_pr_cooling_down` | It stops a PR being linked and completed in one call. Its 600s length (`config.py:48`) is a deployment setting. The real pain with it, CI still running when it expires, is filed as GRPH-1416, which fixes the cause rather than adding a knob. |
| "Run it against real data", "sabotage the call" | `AGENTS.md:124` and nearby | Guidance with no enforceable form beyond `review.sabotage_required`. It stays in `AGENTS.md` (`agents.operating_loop`, channel doc). |

## Machine and run settings (stay flags)

These describe the box or the run, and one project is drained from several machines, so they
are not project rules:

- `REGISTRATION_WINDOW` (90s, `fleet/src/gbfleet/spawn.py:40`): how long a vendor's CLI takes to
  start *on this machine*. GRPH-1400 made it per adapter and per run, which is the right
  granularity.
- `DEFAULT_MAX_WORKERS` (`supervisor.py:59`), the memory reserve and per-child estimate
  (`fleet/src/gbfleet/headroom.py:39`, `:46`): capacity of the box.
- `REVIEWER_FAILS`, `RESPAWN_CEILING` (`fleet/src/gbfleet/until.py:107`, `:2278`), the
  watchdog's `STALL_AFTER_S` (`fleet/src/gbfleet/watchdog.py:26`): how one run protects itself
  from looping. Spend across runs is already `plan.budget_cap`.
- `--merge` (`fleet/src/gbfleet/cli.py:185`): a per-run decision by the person launching the
  drain.

## Filed

The four high-priority candidates are filed in the shape of GRPH-1405 (one rule, its params,
a default that changes nothing on upgrade), each linked to GRPH-1406:

- GRPH-1423: `plan.exclusive_areas`
- GRPH-1424: `build.generated_files`
- GRPH-1425: `build.undeclared_files`
- GRPH-1426: `merge.done_predicates` (depends on GRPH-1059 for removing `suite_green`)

Each library rule reaches a box through the signed rule-pack feed, which lives outside this
repo. Each item therefore also needs its template added there, as `build.test_scope` did.
