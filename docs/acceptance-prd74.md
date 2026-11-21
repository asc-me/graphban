# PRD-74 project rules — acceptance walk (GRPH-1431)

Walked on the real box against a project that is not this repository.

- **Box:** `ubuntu-srv:8080`. The baseline was read on 2026.11.16 with pack 0.4.0. The drain ran on 2026.11.40.
- **Project:** `p74-walk` (key P74W), repo `asc-me/p74-walk`. Migrations live in `db/migrations/`, not `backend/alembic/versions`, and a generated client is checked in under `client/generated/`.
- **Items:**
  - P74W-1 and P74W-2 each add a migration;
  - P74W-3 changes `api.toml` and regenerates `client/generated/client.py`;
  - P74W-4 is the done-gate probe.

The walk was paused on 2026-11-13: the Rules page could not set list params (GRPH-1583). It resumed as the first part of the GRPH-P82 walk ([acceptance-rules-editing.md](acceptance-rules-editing.md)), with every rule set by a person on the page.

## Summary

| # | Walk | Verdict |
|---|---|---|
| 1 | A project that never set a rule behaves as before | **pass** |
| 2a | Exclusive areas: two migration items do not build in parallel | **pass** |
| 2b | Generated files: a regenerated client reaches a PR | **pass, with a caveat**: the generated-files rule itself was not exercised |
| 3a | Dropping a deployment predicate is refused, naming it | **pass** |
| 3b | An item without `scans_clean` is refused at done | **partial**: refused, but by the general attestation gate, which does not name `scans_clean` |
| 3c | Run the drain to done | **not run to done**: p74-walk's CI cannot attest to the box |

## 1. A project that never set these rules

On 2026-11-11 (2026.11.16, pack 0.4.0), `get_rule_toggles(p74-walk)` returned "platform default" as the source for every rule:

- `plan.exclusive_areas` `["backend/alembic/versions"]`;
- `build.generated_files`: the 9 defaults (`.agents/skills/` … `backend/tests/shard_weights.json`);
- `merge.done_predicates` `[]`, so the effective predicates were `suite_green` (deployment, when the item has a linked PR);
- `build.test_scope` `agent_and_pr`, engines `[postgres, sqlite]`.

`gliphy-board`, which never set a rule, read back the same values. Before any rule was set, the dry run put P74W-1, P74W-2 and P74W-3 in three free clusters. That is the old behaviour: nothing joins the two migrations, because `db/migrations` is not the default exclusive area.

## 2. Exclusive areas and generated files

**Set on the page** (2026-11-19, 2026.11.39/40). These values are read back with source `project`:
- exclusive areas `["db/migrations"]`;
- generated files: the 9 defaults plus `client/generated/`.

Two defects turned up while setting them, and both were fixed before the drain ran:
- **GRPH-1727:** "Reset to inherited" kept the saved list.
- **GRPH-1742:** two items that edit different files under one exclusive prefix were not joined.

**Dry run after** (`.walklog/dryrun-rules-on-page.txt`): P74W-1 and P74W-2 are one cluster, "joined on exclusive-area (db/migrations ~ db/migrations)". P74W-3 is free alongside them.

**Drain** (wave p74w2, 2026.11.40):
- **P74W-1** was built on `gb/p74w2-1` @7f18c3d with `db/migrations/0002_tags.sql` and `tests/test_app.py`. It went to review.
- **P74W-2** stayed `next` and was never claimed while P74W-1 was in progress. The log said: "P74W-1 holds db/migrations/0002_tags.sql, tests/test_app.py … Not spawning into work that cannot be claimed".
- **P74W-3** was built on `gb/p74w2-2` @5094727 with `api.toml` and `client/generated/client.py`. It reached a PR (asc-me/p74-walk#2) with no undeclared-file hold.

**Caveat on 2b.** The builder declared `client/generated/client.py` as a touchpoint. A declared file never meets the undeclared-file hold, so this run does not show that `build.generated_files` is what let it through. A separate check is still needed: a build that regenerates a file it did not declare.

**Side observations:**
- P74W-1's first push was held for undeclared files: three `__pycache__/*.pyc` files. The walk repo's `.gitignore` had never been committed. That is the hold working, not a defect.
- The supervisor's PR create hit GitHub's GraphQL rate limit (GRPH-1730). The planner opened asc-me/p74-walk#1 and #2 over REST.

## 3. Done predicates

**Dropping the floor is refused, naming it.** Writing `["scans_clean"]` alone returned this 422:

> merge.done_predicates cannot drop 'suite_green': it is required by the CI floor for items with a linked PR — a project may only add predicates to the floor

`["suite_green", "scans_clean"]` was accepted. It was later set again on the page, with `suite_green` shown as a fixed floor row and `scans_clean` added.

**Refused at done.** Once the PRs were linked and the cooldown had passed, both items were refused:

> refused at `done` (pr_ci_not_green): cooldown over, no suite_green at commit 7f18c3d: no CI run has reported suite_green for it yet

P74W-3 got the same refusal at 5094727. `until` exited with `review-ci-unattestable`. GitHub's `test` job was green on both heads. But p74-walk's CI has no step that attests to the box, and a GitHub runner cannot reach `ubuntu-srv` on the LAN.

**P74W-4 probe.** The planner tried `update_item status=done` with no attestation. It was refused:

> P74W-4 cannot move to done: nothing has attested it. `done` needs an `attestation` receipt …

The item was refused. But the refusal comes from the general attestation gate, not the project's added predicate. Proving that a missing `scans_clean` is what refuses needs two things this walk did not have: an attestation that passes `suite_green` but lacks `scans_clean`, and a key with the gate scope to write it.

## Gaps left open

- **CI attestation from a project on a LAN box:** p74-walk needs a tunnel or a self-hosted runner before any of its items can reach done.
- **A check of an undeclared generated file:** closes caveat 2b.
- **A done refusal that names the missing project predicate:** closes 3b.
- The p74-walk `.gitignore` is still uncommitted.
