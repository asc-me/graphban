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

### `engines` (GRPH-1429)

A second param, `engines`: a list of engine names, default `["postgres", "sqlite"]`, so nothing
changes on upgrade. It applies only in `agent_full`: a merge with no CI attestation counts the
builder's runs only when the item carries a full passing `test` receipt for each listed engine
(e.g. `["mysql"]` is met by one full passing `engine: mysql` run; a SQLite-only run is not).
The supervisor reads the resolved list in place of its fixed pair, and the brief's `Test scope:`
line names the engines under `agent_full`. A value that is not a non-empty list of strings is
refused (422). The rule-pack template must carry `engines` in its default params.

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
