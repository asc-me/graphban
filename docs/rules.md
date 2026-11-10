# Rules

## Installing a rule pack takes effect on every worker (GRPH-1418)

A server can run several uvicorn workers, each with its own copy of the rule library. The
installed pack in the database is the source of truth: at the start of every request, before
anything reads the library (a gate's guard included), the worker compares the installed pack's
sha with the one it loaded (a read at most every two seconds) and reloads its library when they
differ. A pack install, or a replaced rule, therefore takes effect on every worker within a few
seconds without a restart: a rule the new pack adds is offered and enforced everywhere, and a
rule it drops stops being offered and enforced on all of them. The library never changes in the
middle of a request.

## `build.test_scope` — where the test suites run (GRPH-1405)

A per-project library rule with one param, `mode`. Set it on the Rules page (Build stage); a
person sets it with a reason, and the change is recorded in the rule's history like any toggle.
A project that never set it reads `agent_and_pr`, so nothing changes on upgrade.

| `mode` | Builder runs | The PR's CI runs |
| --- | --- | --- |
| `agent_full` — the agent runs CI | the full suites | not required |
| `agent_and_pr` — the agent and the PR run CI (default) | the full suites | the full suites |
| `agent_targeted` — the agent runs partial CI, only what changed | the tests it wrote or changed, the module's existing tests, the sabotage check | the full suites |

The API refuses any other value (422). The delegation brief carries one `Test scope:` line
(and a `test_scope` field) naming the mode and what it asks of the builder or reviewer.

This item only adds the rule and exposes it; the operating loop and skills (GRPH-1403) and the
evidence and merge gates (GRPH-1404) adopt the mode. The rule reaches a box through the signed
rule-pack feed (primitive `test_scope`, template `build.test_scope`, stage Build, default `on`,
`mode = "agent_and_pr"`); the feed lives outside this repo and needs that template added.

### `engines` (GRPH-1429)

A second param, `engines`: a list of engine names, default `["postgres", "sqlite"]`, so nothing
changes on upgrade. It applies only in `agent_full`: a merge with no CI attestation counts the
builder's runs only when the item carries a full passing `test` receipt for each listed engine
(e.g. `["mysql"]` is met by one full passing `engine: mysql` run; a SQLite-only run is not).
The supervisor reads the resolved list in place of its fixed pair, and the brief's `Test scope:`
line names the engines under `agent_full`. A value that is not a non-empty list of strings is
refused (422). The rule-pack template carries `engines` in its default params, which the
`test_scope` primitive declares from primitive API 1.8.0 (GRPH-1494). Before that, a template
with `engines` was refused whole, and one without it refused every write of `engines`.

## `build.undeclared_files` — undeclared files hold the PR or only warn (GRPH-1425)

A per-project library rule (stage Build, level `checked`) with one param, `mode`. A project that
never set it reads `hold`, so nothing changes on upgrade.

| `mode` | A branch with a file outside every declared touchpoint |
| --- | --- |
| `hold` (default) | is held back from a PR; a wait names the files until the touchpoints are widened |
| `warn` | gets its PR; the files are recorded as a wait and as a note on each item |

The API refuses any other value (422). This rule moves only *when* undeclared files are caught.
The merge-time check is unchanged and applies in both modes: every changed file must sit inside
a touchpoint of an item signed off at that commit (`propose.merge` `covering`, GRPH-1408).

The supervisor reads the mode through `get_rule_toggles`; anything unreadable is `hold`. The
note is the `undeclared_warned` kind on `POST /api/fleet/observations`. The rule reaches a box
through the signed rule-pack feed (primitive `undeclared_files`, template `build.undeclared_files`,
stage Build, default `on`, `mode = "hold"`); the feed lives outside this repo and needs that
template added.

## `plan.exclusive_areas` — which file areas are exclusive (GRPH-1423)

A per-project library rule with one param, `paths`, a list of globs. A lease over a path in the
list is exclusive: a second drain cannot take it. Every other overlap is a soft lease, because
git reports a real conflict at merge time (GRPH-1389). The default is
`["backend/alembic/versions"]`, so nothing changes on upgrade. A project whose single-writer
files live elsewhere (`db/migrations/**`, `prisma/migrations/**`) sets its own list on the Rules
page (Plan stage), with a reason; the change is recorded in the rule's history like any toggle.
A trailing glob is ignored: `db/migrations/**` covers `db/migrations/0001.sql`.

The API refuses a `paths` value that is not a list of strings (422).

The merge check reads the same migrations directory. When the merged tree has no
`backend/alembic/versions`, it records `alembic-heads=none` rather than `alembic-heads=1`, so a
check that looked at nothing does not read as a pass.

The rule reaches a box through the signed rule-pack feed (primitive `exclusive_areas`,
template `plan.exclusive_areas`, stage Plan, default `on`,
`paths = ["backend/alembic/versions"]`); the feed lives outside this repo and needs that
template added. The primitive API is 1.7.0 (a `list` param type).

## `build.generated_files` — files the undeclared-file hold ignores (GRPH-1424)

A per-project library rule (stage Build, level `checked`: only the supervisor honours it) with
one param, `paths`: exact paths, `/`-ending prefixes, or globs (`client/generated/**`). A change
outside an item's touchpoints that matches one of them is logged as `generated_files_allowed`
and does not hold the PR; any other undeclared file does. The default is the list the supervisor
always had (`AGENTS.md`, `.claude/agents/`, `.agents/skills/`, `backend/tests/shard_weights.json`, …),
so nothing changes on upgrade. The supervisor reads the resolved rule at reap time; when it cannot
(server down, rule absent from the pack, a client not allowed `get_rule_toggles`) it uses that list and emits `generated_files_rule_unread`
with the reason. The rule reaches a box through the signed rule-pack feed (primitive
`generated_files`, level `checked`, enforcer the supervisor; template `build.generated_files`,
stage Build, default `on`, `paths` = the list above). The primitive exists from primitive API
1.8.0 (GRPH-1494).

## `merge.done_predicates` — predicates `done` requires (GRPH-1426)

Stage Merge, primitive `done_predicates` (enforced), one param, `predicates` (a list of names).
The done gate — `update_item` to `done` and `sign_off` alike — requires a passing attestation
at the item's head for each name in the project's effective list. Items already `done` are not
re-checked. A predicate that did not run is not a pass, and it must be attested by a
`gate`-scope key.

- **Unset (empty list):** the project follows the deployment's `REQUIRED_PREDICATES` live, plus
  `suite_green` for an item with a linked PR (the PR cooldown gate). Today's behaviour.
- **Set:** the deployment's entries and `suite_green` are a floor. The list must contain all of
  them; a person may only add predicates, with a `reason`. A list that drops one is refused 422
  naming the predicate and its source.
- **Audit:** `GET /api/rules/toggles` returns `effective_predicates` for this rule — each name
  with its source (`deployment` or `project`). The rule history records who set the list and why.

## `gitops.merge_method` — how the supervisor merges (GRPH-1428)

`squash` (default when unset), `merge` or `rebase`. Set it on the gitops PATCH like the other
`gitops.*` fields; any other value is a 422. It rides `get_context` under `gitops` as
`merge_method` (`{value, source}`), read where `base_branch` is, so a linked box gets the
cloud's rule; the supervisor passes the matching `gh pr merge` flag. A branch that
is behind is updated with `gh pr update-branch --rebase` under `squash` or `rebase`, and with a
plain merge update under `merge`. The PR head must still be the reviewed commit, whatever the
method. A server that predates the field, or an unreadable value, reads as `squash`.

## Which pack version carries each rule (GRPH-1430)

A rule absent from a box's pack reads as its default, never as off (`backend/tests/test_rule_pack_p74.py`).
A project can only *change* a rule once the box's pack carries it.

| Rule | First pack | Needs primitive API | Needs server |
| --- | --- | --- | --- |
| `build.test_scope` (`mode`) | 0.2.0 | 1.6.0 | 2026.11.11 |
| `plan.exclusive_areas` | 0.3.0 | 1.7.0 | 2026.11.12 |
| `build.undeclared_files` | 0.3.0 | 1.7.0 | 2026.11.12 |
| `merge.done_predicates` | 0.3.0 | 1.7.0 | 2026.11.12 |
| `build.generated_files` | 0.4.0 | 1.8.0 | the first release carrying GRPH-1494 |
| `build.test_scope` `engines` | 0.4.0 | 1.8.0 | the first release carrying GRPH-1494 |

`gitops.merge_method` is a gitops field, not a library rule, and needs no template.
