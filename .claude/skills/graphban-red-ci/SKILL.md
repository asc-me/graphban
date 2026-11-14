---
name: graphban-red-ci
description: >
  Triage a red CI run and act on the standing decisions instead of asking. Use
  when a PR's CI is red, a drain is stuck on red CI, `suite_green` is missing
  on a head, or the user runs /graphban-red-ci.
user-invocable: true
---

# Triage a red CI run

A red run is usually already decided. AGENTS.md "Standing decisions" answers it. Apply that answer, record what you did as evidence on the item, and say so in your report. Do not ask a person for an answer that is already written down.

## Read the run

`gh run view <run-id> --log-failed`. Name every failed job and test id. Then put each failure in exactly one class below. Rerun only the tests CI named, on the engine that failed. Do not rerun the whole suite to find out.

## The four classes

- **Regression.** Signs: the named test fails the same way every time, alone and on a rerun. Action: fix the code, not the test. Send it back to its builder with `bounce` and the test id, or let `merge.reopen_on_red` reopen it after a merge.
- **Load-sensitive test.** Signs: it passes alone or on a rerun, it failed only on a shared host, and the failure is a time delta, a wall-clock budget or a `findBy` timeout. Action: file or claim a test-fix item. Make the test assert what it means: an ordering, a count, an injected clock, a cost relative to a baseline measured in the same process, or an explicit longer wait where the wait is real (GRPH-1536 has worked examples).
- **Infra.** Signs: DNS, a runner that died, a cache or `setup-*` post step, an image pull. Action: rerun the job once (`gh run rerun <run-id> --failed`) and add the evidence to the open item for that infra fault, or file one. If the same job fails the same way twice, file the item and say so.
- **Stop and ask.** A security-scan finding, a migration or its data effect, or a change to product behaviour. These are a person's call every time. Name the run, the finding and the item, then stop.

## Never, to get green

Never skip a test. Never raise a budget or a timeout. Never add a retry. Never shrink the runner pool. Each of these hides the failure instead of fixing it.

## A test fix is still a change

A test fix needs sabotage-red evidence: break the thing the test guards, show the test go red (`tests_failed` above zero), restore it. It also needs a second agent's review. Do not sign off your own fix.

Tell the user each failure, its class, and what you did. Then stop.
