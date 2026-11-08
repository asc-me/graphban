# Rules

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

## `build.generated_files` — files the undeclared-file hold ignores (GRPH-1424)

A per-project library rule (stage Build, level `checked`: only the supervisor honours it) with
one param, `paths`: exact paths, `/`-ending prefixes, or globs (`client/generated/**`). A change
outside an item's touchpoints that matches one of them is logged as `generated_files_allowed`
and does not hold the PR; any other undeclared file does. The default is the list the supervisor
always had (`AGENTS.md`, `.claude/agents/`, `.agents/skills/`, `backend/tests/shard_weights.json`, …),
so nothing changes on upgrade. The supervisor reads the resolved rule at reap time; when it cannot
(server down, rule absent from the pack, a client not allowed `get_rule_toggles`) it uses that list and emits `generated_files_rule_unread`
with the reason. The rule-pack feed (outside this repo) needs the template `build.generated_files`
added, as for `build.test_scope`.

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
