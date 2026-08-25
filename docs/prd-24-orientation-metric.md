# How orientation cost is measured (PRD-24 §9.3, settled)

PRD-24 left three questions open after two grill rounds. This settles the third, because S6
claims an improvement and **a claim without a metric is unfalsifiable**.

> **How is orientation cost measured?** Counting filesystem tool calls per run is the obvious
> proxy, but a run that greps once cleverly beats one that calls `code_neighbors` three times,
> and the metric would call the second better.

## The obvious proxy is rejected, and the grill was right about why

Counting graph calls versus filesystem calls measures **the means, not the end**. It would
score a run that made three `code_neighbors` calls above one that made a single well-aimed
`grep` — and the second run is better by every measure anyone actually cares about. Worse, it
is trivially gamed: an agent told "prefer graph calls" can raise the number without learning
anything, and the metric would applaud.

It also embeds the conclusion in the measurement. If the number that defines success is "how
often did you use the tools we added", then the tools we added cannot fail.

## The metric

**Turns to first write, on runs that finished.** Recorded as `Outcome.turns_to_first_write`.

Latency is the design constraint — 22.2s, 29.7s and 44.8s per turn for the three measured
models — so **the turn is the unit of cost**, and the question orientation answers is "how many
of them went by before this agent started changing anything".

This gets the grill's edge case right. A run that greps once and edits on turn 3 scores 3. A
run that calls `code_neighbors` three times and edits on turn 5 scores 5. The first is better
and the metric says so, without caring which tools were used to get there.

**Two guards, because the metric is gameable on its own:**

1. **Only runs that finished count.** Editing on turn 1 is trivially achievable by editing
   blindly, and a blind edit does not reach a passing test suite. `status == "finished"` means
   the model stopped asking for tools; on a repository with a declared test command that means
   it had run them.
2. **`None` is not zero.** A run that never wrote anything has `turns_to_first_write is None`,
   and averaging it as `0` would make the worst possible run — one that read for forty turns
   and changed nothing — look like the best one. This is the same discipline as
   `verify.run_tests` refusing to report `failed: 0` on output it could not parse.

**Total turns is reported alongside it** and neither replaces the other: first-write measures
orientation, total measures the whole run.

## What this metric is not

It is **not the arc's success metric.** PRD-24 §9.2 names that separately and it is still
open: *items signed off without a bounce*. An agent that orients perfectly and produces
plausible, wrong edits scores well here and is worthless. Orientation cost is a narrow
question — is the graph cheaper than crawling — and this answers only that.

It is also **not comparable across repositories or models.** A 44.8s-per-turn model and a
22.2s one have the same turn count and very different runs; a repository with no code map has
nothing to orient against. Comparisons are only meaningful with the model, the repository and
the task held fixed — which means the honest form of this measurement is an **A/B on one item:
the same run with and without the orientation instruction.**

## Status

The instrument ships in S6 and is recorded on every run. **The number itself is not claimed
here.** Measuring it honestly needs a real repository that is actually indexed in the graph and
a real item, which is S7's acceptance walk — and per PRD-24 §8, that walk should be allowed to
fail. If turns-to-first-write does not drop with the instruction, the instruction is not
earning its tokens and should be cut.
