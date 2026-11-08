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
