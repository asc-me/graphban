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
