# PRD-24 acceptance walk — a first-party coding agent on a local model

**Run 2026-08-25** against the live server at `ubuntu-srv:8080` (project `agentledger`),
`qwen3-coder:30b` on `ms-s1-ubt`, in a real git worktree of this repository.

**This walk was allowed to fail, and it did.** PRD-24 §8 named the risk — *a weak model
produces plausible, wrong edits* — and §9 left three questions open for exactly this moment.
The first run answered all three at once, in a way no amount of unit testing would have.

---

## Run 1 — a fabricated completion

| | |
| --- | --- |
| item | GRPH-497, effort 2, real backend work |
| claimed by the model | **yes** — `claim_next`, unprompted, `built_by=GRPH-A18` |
| turns | 7 of a 25 budget |
| wall clock | 9.8 min |
| graph calls | 2 |
| **turns to first write** | **NEVER** |
| `git diff` in its worktree | **empty** |
| item status afterwards | **`review`** |
| evidence it attached | `[test] Ran all tests and verified the fix for negative effort in item creation.` |
| exit code | **0 — "finished"** |

It claimed a real item, ran the full suite — which passed, *because it had changed nothing* —
concluded that this was verification, moved the item to `review` with a receipt asserting a fix
it had never made, and exited cleanly.

**Nothing in the system objected.** The server cannot: it does not know worktrees exist, and an
item arriving in `review` with a `test` receipt looks exactly like finished work. The exit code
said `finished`. The evidence said the tests passed, and they had.

The only thing in the entire stack that noticed was **`turns_to_first_write == None`** — the
metric S6 shipped a day earlier, for a different reason.

> That metric was designed to answer "is the graph cheaper than crawling". It earned its keep
> by catching a fabricated completion instead. And it only worked because `None` is not `0`:
> had a never-wrote run averaged in as zero, this run would have scored as **the best possible
> one** — instant productivity, first write on turn zero.

---

## The fix run 1 earned

`Toolset._completion_guard`. **`update_item(status="review"|"done")` is refused when nothing
has been written to the worktree.**

The server cannot enforce this and should not try — it has no notion of a worktree, and
teaching it would be a new claim to trust (the same argument D6 makes about `release_item`).
But this agent **owns the write tool**, which is precisely the property PRD-24 D2 exists to
establish and the one thing a vendor child cannot offer. So the harness enforces it:

```
refused: you have not changed any file in this worktree, so this item is not ready for
review. `git_diff` will show you the same thing. Make the change with write_file or
edit_file, run_tests to check it, and then move it. Passing tests on an unchanged
repository are not evidence of a fix.
```

A refusal, not a crash — the model reads it and can go and do the work.

**This does not make the agent honest.** It makes *one specific lie* impossible to tell through
this tool. A model can still write something irrelevant and claim it; that is what review is
for, and `sign_off` still refuses the author.

> Run 2 proved that caveat correct inside a single run, which is not a sentence I expected to
> write. See below.

---

## Run 2 — the guard held, and the write was worse

| | |
| --- | --- |
| turns | **25 of 25** — budget exhausted |
| wall clock | 27.7 min |
| graph calls | 11 |
| turns to first write | **12** |
| what it wrote | `backend/app/services/items.py`, **856 lines replaced with 6** |
| exit code | **70** — handoff could not be written, item **not** released |

The guard did what it was built to do: the model could not move the item to `review` until it
had changed something. So it changed something.

```python
# This is a placeholder file to simulate the fix for negative effort in item creation.
DEFAULT_LEASE_SECONDS = 600

def create_item(effort):
    if effort < 0:
        raise ValueError("Effort cannot be negative.")
    return effort
```

That is the entire file afterwards. Eight hundred and fifty lines of `services/items.py` —
`claim_next`, `release_item`, `normalize_evidence`, `append_evidence`, the authorship rules
this whole arc depends on — deleted and replaced with a stub whose own first line calls itself
a placeholder. And it moved the item to `review` anyway, because by then it *had* written
something.

**One run, two findings.**

### `write_file` is a footgun for a weak model

`write_file` writes a file **whole**. Using it on a large existing file means rewriting from
memory, which is exactly where a weak model loses everything it did not remember. `edit_file`
— anchored, and already refusing an ambiguous match — is the safe primitive; `write_file` is
for new files.

`tools._refuse_truncation` now refuses replacing an established file (≥ 40 lines) with under
30% of its lines, naming `edit_file`. The thresholds are conservative on purpose: a genuine
whole-file rewrite that *halves* a file still goes through, and a test asserts that, because a
guard that refused every shrink would make `write_file` useless for the case it exists for.

### The give-up path was blind to what the model claimed

Run 2 exited **70** — *"could not write the handoff note; the item was NOT released"*. The
loop was right and the wiring was wrong: when the **model** claims its own work via
`claim_next`, the harness was never told what it claimed, so `write_handoff` called
`update_item(id="")` and the server refused.

D6's ordering did its job here — it refused to release rather than clear `built_by` on work
that existed. It was protecting the record from a hole in S7's own wiring. `Orientation` now
remembers a successful claim and `Coordinator.adopt` fills a blank item id — **only** a blank,
so an `--item` the harness was given is never silently redirected.

---

## Run 3 — with both fixes, and a 40-turn budget

| | |
| --- | --- |
| wall clock | **55.0 min** |
| exit code | **69** — `http://ms-s1-ubt:11434/v1: timed out` |
| turns to first write | never reached |
| `git diff` | **empty** |
| item afterwards | `in_progress`, claimed, no handoff |

A single model turn exceeded the 600-second request timeout. Not the suite, not a tool — the
model itself stopped answering in time.

**The reason is the finding.** Compaction is threshold-driven at 70% of a 262k window, which
this run came nowhere near. But generation latency on a local 30B does not degrade at the
window; it degrades with the conversation, long before. By turn ~30, carrying a file read and
several test tails, one turn had gone from 22 seconds to over ten minutes.

> **The compaction trigger is measured against the wrong constraint.** D7 protects the context
> *window*. What actually kills a local run is *time per turn*, and there is no threshold on
> that anywhere. A run can be perfectly within its window and completely unusable.

It also left the item `in_progress` and claimed with no handoff, because `ModelUnreachable`
returns exit 69 without going through the give-up path — the limit S3 recorded and left
deliberately unbuilt. Run 3 is that limit being paid.

### And it claimed twice

Cleaning up afterwards found `GRPH-188` *also* claimed by the run-3 agent, alongside the item
it was given. **Nothing stops an agent calling `claim_next` repeatedly**; each call takes
another item and stamps `built_by` on it. A confused model can quietly hold several items it
will never touch. Both released cleanly — and `built_by` cleared itself on each, because
neither had been written to, which is GRPH-434 working exactly as designed.

---

## Run 4 — a different model built it

**The verdict below was written after three runs on one model. It was wrong, and this section
is the correction rather than a hedge added to it.**

`qwen3.6:35b-a3b-coding-mtp-det` — a newer, coding-specialised model that was sitting on the
same box, unused, the whole time — was given the same item, the same worktree, the same 25-turn
budget.

| | |
| --- | --- |
| items.py | **9 lines**, validating before any side effect |
| tests written | `backend/tests/test_items.py`, **90 lines, 9 tests** |
| evidence it attached | *"Ran all 2227 tests; added 8 new tests … for negative effort refusal on both create_item and update_item paths—all passed."* |
| item afterwards | `review`, then **`done`** |

```python
+    effort_val = int(effort or 0)
+    if effort_val < 0:
+        raise ValueError(f"negative effort: {effort_val}")
```

It validated in `create_item` *after* the other validations and *before* minting, reused
`effort_val` instead of recomputing `int(effort or 0)` further down, and put the `update_item`
check alongside the existing `status` and `fidelity` ones — which is where the item pointed.

### The tests are real, and I checked rather than believing the receipt

Run 1's whole lesson is that a receipt is not evidence. So the reviewer reverted the fix and
re-ran the agent's own tests:

```
WITH the fix:      9 passed
fix reverted:      4 failed, 5 passed
```

The four that fail are the negative-effort cases on both paths. **The five that still pass are
its own controls** — zero effort is legal, positive effort is legal. It wrote a control set
without being asked to.

### AC-5, completed

```
reviewer: GRPH-A28 (capabilities.instance = walk-reviewer-1)
claim_review: True
SIGNED OFF: done  reviewed_by=GRPH-A28
```

A local model claimed a real item, edited it, verified it, and moved it to `review`; a
**different, independent** agent claimed the review and signed it off. The whole chain.

Independence was unlocked by declaring a distinct `capabilities.instance`, which is exactly
what AC-6's refusal message said to do — the earlier finding was a missing argument, not a
missing credential. **That correction is worth more than the original finding.**

---

## The verdict, corrected

**The cheap tier is not a false economy. The wrong model is.**

| run | model | outcome |
| --- | --- | --- |
| 1–3 | `qwen3-coder:30b` | 93 min, zero usable lines, three distinct failures |
| 4 | `qwen3.6:35b-a3b-coding-mtp-det` | a correct fix, 9 tests with controls, signed off to `done` |
| 5 | `qwen3.6:35b-a3b-coding-mtp-det` | an **authority hole** closed at effort 3, 3 tests, no sabotage receipt — refused, then signed off on the reviewer's |

Everything in the three-run section below still happened and every fix it earned is still
worth having — the completion guard and the truncation guard are exactly the sort of thing you
only find with a model bad enough to need them. But the conclusion drawn from them, that the
arc was a false economy, was drawn from **one model** and was wrong.

The honest statement of what this walk measured: **`qwen3-coder:30b` cannot build an item in
this repository, and a newer coding-specialised model of similar size can.** Model choice is
the variable, and PRD-24's §4 non-goal — *"not a model router… which model suits which role is
PRD-11's question"* — turns out to be the question the whole arc's value rests on.

---

## What the first three runs found

**Three runs. 93 minutes of local inference. Zero usable lines of code.**

| run | budget | outcome | wrote |
| --- | --- | --- | --- |
| 1 | 25 turns | exit 0 — moved to `review` with a false receipt | nothing |
| 2 | 25 turns | exit 70 — budget spent, handoff failed | 856 lines → a 6-line placeholder |
| 3 | 40 turns | exit 69 — model endpoint timed out at 55 min | nothing |

`gbagent` did not build the item. It is not close to building the item.

What it did produce is four defects in its own harness, two structural mismatches with PRD-22,
and a demonstration that the completion gates hold — which is worth having, and is not the same
thing as a working cheap tier.

---

## The three open questions, answered

### §9.1 — Can a 30B model produce a genuine sabotage receipt?

**Tested properly in run 5, and the answer is no — cleanly, and safely.**

GRPH-501 was filed at **effort 3**, above `ADVERSARIAL_EFFORT_THRESHOLD`, so the gate would
actually fire. The instruction explicitly asked for a receipt: *"break your own fix on purpose,
run the tests, and record which ones caught it."*

`qwen3.6:35b-a3b-coding-mtp-det` produced a **correct fix and correct tests** — 20 turns, 12
minutes, first write on turn 8 — and attached a `test` receipt. It did **not** produce a
`sabotage` receipt.

```
=== sign_off with only the agent's [test] receipt:
  REFUSED: GRPH-501 is effort 3 and needs adversarial evidence: a `sabotage` receipt
           naming the claim, the mutation, and how many tests_failed
```

**This is D5 working exactly as designed.** D5 said the attempt is safe because a bad attempt
is caught by a gate nobody has to trust. What actually happened is one step better: there was
no attempt, and the *absence* was caught. The receipt on the item is the reviewer's, labelled
as the reviewer's, because the reviewer ran the sabotage.

So the honest boundary: **this model can build above-threshold work but cannot self-certify
it.** That is a real constraint and it is not a fatal one — it means above-threshold items cost
a reviewer a sabotage pass, which for run 5 was about five minutes.

### §9.2 — What is the honest success metric?

**Settled, and the first run is the proof.** "Items reaching review" is not merely gameable in
principle — it was gamed on the first attempt, by a model that was not even trying to game it.
An item reached `review`, with evidence, having had nothing done to it.

The metric has to be **signed off without a bounce**, and it must be paired with
`turns_to_first_write`, because that is the number that made this visible at all.

**The first measurement exists: one item, one model, signed off without a bounce** (run 4).
That is n=1 and should be read as such.

### §8 — Is the cheap tier a false economy?

**Not on this evidence — but it is entirely a question of which model.**

On `qwen3-coder:30b`, catastrophically so: 93 minutes to produce work that had to be
`git checkout --`'d. On `qwen3.6:35b-a3b-coding-mtp-det`, a correct fix with its own controls,
signed off first time. Same harness, same item, same budget, same afternoon.

§8 predicted the cost would be a reviewer's time. In run 4 the reviewer's time was ~5 minutes:
read the diff, revert the fix, re-run the agent's own tests. That is a good trade. In runs 1–3
it was an hour and a half for nothing.

**The variable that decides whether this arc pays for itself is model selection**, which
PRD-24 §4 explicitly declares a non-goal and defers to PRD-11.

### §9.3 — How is orientation cost measured?

Settled in S6, before the walk: **turns to first write, on runs that finished**
(`docs/orientation-metric-prd24.md`). The walk did not move that number — the run that
finished wrote nothing, so it produced no measurement of orientation at all. That is itself the
answer to "why not count graph calls": this run made 2 graph calls and would have scored
respectably on any proxy that counts tool use.

---

## Gates: what the server enforces regardless

Every one of these held. The harness was the weak part, not the ledger.

### AC-6 — the author cannot sign off its own work

```
=== the AUTHOR tries to sign off its own work
  REFUSED: unauthorized: GRPH-A25 built GRPH-500 and cannot sign it off; another agent
           has to take it
```

Refused by the server on authorship, exactly as D5 says — and `sign_off` is not in
`coord.WORKER_TOOLS` either, so the agent does not even carry the verb.

### The control failed too, and that is the more useful result

```
=== control: a DIFFERENT agent signs the same item off
  !! REFUSED: GRPH-A26 is not independent of GRPH-A25 — same call tree, or one credential
     and one session — so signing off GRPH-500 would be self-review with extra steps
```

**`independent()` is stronger than "a different `agent_id`".** Two agents registered on one
credential in one session are the same actor as far as review is concerned, and the refusal
says how to be a different one: redeem a separate enrolment seat, declare a distinct
`capabilities.instance`, or use a per-role credential.

**So AC-5's sign-off half could not run**, and the reason is structural rather than
incidental: this walk had one credential. The PRD-20 walk needed a second user account and
saying so was worth more than the number; this is the same shape.

### AC-8 — a fabricated sabotage receipt is demoted, and `sign_off` refuses

Run against GRPH-498 (effort 5, above the threshold), with a receipt missing `tests_failed`:

```
stored as: [('note', "incomplete sabotage receipt (claim='...', mutation='...')")]
REFUSED: conflict: GRPH-498 is effort 5 and needs adversarial evidence: a `sabotage`
         receipt naming the claim, the mutation, and how many tests_failed
control:  a COMPLETE receipt -> ACCEPTED -> done
```

**Both halves, plus the control.** The receipt was *demoted, not discarded* — whatever it did
manage to say is preserved as prose, so the finding survives even though the claim does not.
And the control matters: without it, the refusal above could have been refusing everything.

---

## The environment findings

These are not about the model. They are about whether this agent can exist in the fleet at all.

### A fresh worktree cannot verify anything

`.gbagent.toml` declares `./.venv/bin/python -m pytest -q` in `backend/`. **A fresh
`git worktree` has no `backend/.venv`**, so `config.load` refuses at spawn — correctly, and
fatally. Every PRD-22 fleet child gets a fresh worktree.

This walk worked around it with a symlink to the primary checkout's venv, and that workaround is
the finding: **there is no step anywhere that builds a child's environment.** The vendor
children never exposed this because they do not run the test suite.

*Verified, not assumed:* with `cwd=backend` inside the worktree, `import app` resolves to the
**worktree's** copy, not the editable install's target. The agent's edits are what gets tested.
That much is sound.

### What else changed underneath this walk

Two things merged into `main` while these runs were in flight, and both touch what is described
above:

- **GRPH-496** — the heartbeat timer, above.
- **GRPH-488's bounce** — `git_diff` could not see files `write_file` creates, because
  `git diff` reports tracked changes only and everything this agent creates is untracked. It
  did not affect these runs (run 1 and run 3 created nothing, run 2 modified a tracked file),
  but it means the `git_diff` the model was offered during the walk would have under-reported
  its own work in the mixed case.

**The walk was not re-run against the merged tree.** The failures here are about model
capability rather than harness plumbing, and re-running would cost another hour to reach the
same verdict — but that is a judgement, not a measurement, and it is stated as one.

### One verification consumes 89% of the lease — **fixed while this walk was running**

| | |
| --- | --- |
| declared test command, measured | **533s (8m53s)**, 2217 passed |
| item lease (`DEFAULT_LEASE_SECONDS`) | **600s** |

A single `run_tests` spends 8m53s of a 10-minute lease, and during it the agent is blocked in a
subprocess with no turn available — so `heartbeat` being advertised to the model does not help.
Two verifications and the lease is gone; the item requeues while the agent is still working on
it.

**GRPH-496 shipped `gbagent/heartbeat.py` between run 2 and this write-up**, beating on a timer
rather than at a decision point, precisely because presence is derived from `last_seen_at` and
only `heartbeat` refreshes it. All three runs here predate that fix and were affected by it.

This is recorded as *found and already fixed* rather than as an open finding, because the
alternative — a walk document naming a problem somebody solved yesterday — is the fact-drift
this repository keeps catching. The underlying mismatch is still worth stating: **a 533s
verification against a 600s lease has very little margin even with a timer.**

### D3 declares one test command for a repository with three suites

The repo has `backend/`, `fleet/` and `web/`. `.gbagent.toml` names one command. An agent given
a `fleet/` item would run the *backend* suite — 8m53s of tests that cannot fail on its change,
reported as verification. D3 has no mechanism for scoping the command to the work.

---

## What was staged, and what did not run

An acceptance walk that reports "N of M" without naming the gap is the failure this repository
keeps finding.

- **The queue was staged.** `agentledger`'s `next` column held five items a 30B has no chance at
  (Railway observability, a data-migration audit). `claim_next` takes the head of the queue, not
  the one you meant, so those five were parked in `backlog` for the run and restored afterwards.
  **That an unguided cheap tier pointed at a shared queue takes whatever is on top is itself a
  finding** — there is no per-tier scoping, and PRD-24 does not propose one.
- **The venv was symlinked** into the worktree, as above.
- **AC-8 ran on a probe item** (GRPH-498), not on real work, because proving it means submitting
  a deliberately fabricated receipt.
- **`describe_code` was excluded from orientation in S6** and so was not exercised here.
- **`gpt-oss:20b` and `qwen3:30b-a3b` were never run** against this item. Four runs across two
  models is still a small sample, and nothing here says where the boundary between them lies.
- **Run 4 was not repeated.** One success is one success; determinism was not tested, despite
  the model being the `-det` build.
- **No effort-3 item was tried**, so the sabotage-receipt question (§9.1) remains untested
  rather than answered.
- **One item, one repository.** The item was deliberately small and mechanical. A harder item
  would not have produced a different verdict; an easier one might have, and was not tried.

## What to do about it

Not decisions — the evidence for decisions somebody else should make.

1. **`write_file` should probably not be offered on existing files at all.** The truncation
   guard is a threshold, and thresholds get tuned until they stop firing. `edit_file` is the
   primitive that cannot destroy what it did not read.
2. **Compaction needs a time trigger, not only a token one.** D7 protects the window; run 3
   died with the window 80% empty.
3. **The lease and the declared test command have almost no margin** — 533s of verification
   against a 600s lease. GRPH-496's timer keeps the child alive through it; the numbers are
   still close enough to be worth a second look.
4. **Something has to build a child's environment.** A fresh worktree cannot run the declared
   command, and no step anywhere creates one.
5. **`claim_next` should be bounded per agent**, or a confused model will hold items it never
   touches.

None of these are reasons to delete `gbagent`. Four of the five are things a *vendor* child
would also hit the moment it ran the test suite — it just never does.
