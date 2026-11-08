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
