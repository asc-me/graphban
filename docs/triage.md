# Triage

**Triage** answers two questions: what came in that nobody has dealt with, and what a new claim
would collide with. Open it from the nav under Triage. It reasons over the current project only.

## What the page is for

- The **incoming queue** holds untriaged requests, and items where an agent is waiting on an
  answer (a question with options, or an open question).
- The **clusters** side shows non-colliding work already in flight, so you can see which claims
  overlap on the same code before they become a merge problem.

A package name shared across repos is a galaxy edge, not a collision. This screen does not compute that.

## Main actions

- **Accept** a request to Backlog or to Next, which files it as an item.
- **Decline** a request (the submitter is notified), **snooze** it, or **merge** it into a
  duplicate (its votes carry over and the report closes).
- **Answer** an agent's question. With options, pick one; for an open question, type the answer
  the agent should work from. An empty answer is not sent. The agent resumes once answered.
- **Escalate** to the project owner, **mark unblocked**, or **release** a stuck claim back to Next.
- On a cluster, **hold** one item behind another (it queues until the first merges) or **allow
  parallel** (an override that permits overlapping claims; project owner only).
- A just-made change can be undone from the toast. A **Processed** count shows what you cleared
  this session.

The page also notes whether a drain is running; if none is, it shows the command to start one.

## Reading an empty result

- An empty incoming queue means everything reported has been triaged. It does not mean nothing
  was ever reported: history lives in the tracker.
- Empty clusters mean nothing is claimed, so there is nothing to collide. That is an idle project,
  not a cleared one: the check found no work, not no conflict.
- "Triage clear" appears only when nothing is waiting.

## Related

[requests-and-feedback.md](requests-and-feedback.md), [fleet.md](fleet.md), [tracker.md](tracker.md).
