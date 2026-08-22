# PRD-17 acceptance walk — a real fleet, four terminals

D1–D9 are shipped and tested. **Everything they assert was verified by tests I wrote against
code I wrote.** This walk is the first time a human drives it, and this repo's history says
that is where the real defects are: the hosted instance that sat live and unreachable for
twelve days, the password button that did not exist, the fork-detection test that passed with
its matching entirely broken.

So run it looking for the things a test structurally cannot see, not to tick boxes.

---

## Setup — enrolment, not per-role keys

**This changed under the walk.** PRD-19 shipped between step 1 and step 2, so the provisioning
half is different from what steps 1 and 15 were run against. The mechanics steps 2–14 test are
unchanged; how you stand the fleet up is not.

Run it on **`ubuntu-srv`** — `http://192.168.50.81:8080/fleet` — which is on the current
revision with a real 431-item backlog. A populated backlog makes steps 4 and 14 mean something
that a scratch stack cannot: real touchpoint overlap to arbitrate.

**Once, ever — the credential.** Settings → API Keys → create one, and put it in your client
config as a single server:

```json
{ "mcpServers": { "graphban": {
    "url": "http://ubuntu-srv:8080/api/mcp",
    "headers": { "X-API-Key": "<your key>" } } } }
```

You do not rewrite this per wave, and **End wave never touches it**.

**Per wave — the seats.** Fleet view → *Provision a whole wave* → add one seat per agent
(**two workers means two `+ worker` clicks**) → *Issue the seats*. Each row copies a filled
prompt with the code already in it. Paste one per terminal.

You need **four terminals**, ideally different vendors — step 6 is cross-vendor review and an
all-Claude fleet cannot demonstrate it. Two is enough for steps 1–9 if that is what you have;
note which you ran with.

---

## Four things that will look like bugs and are not

**1. An agent with no seat is `all-in-one`, not a worker.** That is the single-agent posture:
you are the reviewer and no gate applies. So **step 3 refuses nothing unless the agent redeemed
a seat**. The roster flags this — a row reading `not enrolled` in red is an agent that never
got its code. Check that before concluding the gate is broken.

**2. Two agents on ONE seat cannot review each other, by design.** A seat is a session. If you
paste the same code into two terminals the second is refused outright; if you somehow got two
agents onto one seat they would read as one opinion. Two workers need two seats.

**3. `claimed: false` is usually the correct answer.** From `claim_review` it means nothing is
waiting *that you may take*; from `claim_cluster` it means every ready cluster collides with
work in flight. Both carry a `reason` — read it. Neither is an error.

**4. Presence and item-reclaim run on different clocks.** An agent reads `offline` after
`lease_seconds / 4` (150s at the default), but its item stays held until the full lease (600s).
Step 10 needs ~2.5 min for the first and ~10 for the second. That gap is deliberate: show the
death at once, keep the half-finished work reserved in case the process is merely wedged.

---

## The walk

| # | Step | Expected | Result |
| --- | --- | --- | --- |
| 1 | Issue a wave of 4 seats | 4 seats, one per agent, 30-min expiry, each `unused` | ✅ (as keys, pre-PRD-19) |
| 2 | Each terminal registers | Roster shows 4 distinct ids, correct roles, counts broken out by role |✅ roles + ids correct — **found 2 heartbeat defects** (not filed — fixed in `4446fe7`) |
| 3 | A worker calls `update_item(status="done")` | `unauthorized` naming `reviewer`; item stays `review`; refusal in Activity with the human principal |❌→✅ **FAILED: gate unreachable** (not filed — fixed in `4446fe7`); fixed, re-run 03:39, first firing ever |
| 4 | Two workers `claim_cluster` concurrently | Zero shared touch-areas between the held clusters |✅ two workers, zero shared areas, real concurrent claims |
| 5 | A worker tries `claim_review` on its own item | Refused — and a worker cannot call `claim_review` at all, which is stronger than the PRD's criterion |✅ refused at the role gate |
| 6 | Reviewer signs off | `reviewed_by != claimed_by`; where the fleet has the diversity, reviewer vendor ≠ author vendor |✅ one credential, one host — independent by SEAT |
| 6b | Reviewer signs off an **effort ≥ 3** item with no sabotage receipt | Refused (`conflict`), naming what is missing; passes once a receipt with `tests_failed ≥ 1` is recorded |✅ real sabotage receipt on effort 5 |
| 7 | Promote a worker to reviewer **while it holds its own item in `review`** | Both gates refuse — `claim_review` filters it out, `sign_off` asserts authorship |✅ both gates refused |
| 8 | Re-task a live worker → reviewer | Takes effect on its **next poll**; no reconnect; Fleet view shows the directive pending, then acked |✅ the one I bet against — it arrived on a heartbeat, once |
| 9 | Reviewer bounces an item | Returns to `next`, invisible to other workers until the pin lapses (one lease), then claimable by any |✅ — found GRPH-378/379 |
| 10 | Kill a worker mid-lease | `offline` within ~150s; item back to `next` at ~600s; reservation expired; orphaned branch flagged in its row |✅ offline at t+120s, lease and reservation lapsed — **branch never flagged** (GRPH-396) and the item never re-offered (GRPH-397), both since fixed |
| 11 | **End wave** | This wave's SEATS revoked; leases and reservations released; **your credential still authenticates** and the config is untouched; live agents get a `session_expired` directive on their next poll |✅ 4 seats + 2 legacy keys revoked, 2 leases released, **credential survived** |

**New since the PRD was written — worth checking too:**

| # | Step | Expected | Result |
| --- | --- | --- | --- |
| 12 | Have an orchestrator spawn a subagent that registers with `parent_agent_id` | The subagent cannot `claim_review` or `sign_off` its parent's work |✅ refused while holding a reviewer seat |
| 13 | Two terminals on **one** key, same machine, both reporting `host` | Neither can review the other; the reason says to mint a per-role credential |superseded — seats decide independence now |
| 14 | A worker calls `claim_cluster(wait_seconds=60)` on an empty backlog | Parks; returns within the window when work appears; **one** tool call, not twelve |✅ returned at 8.1s, one call |
| 15 | Register with **no seat at all** | Roster shows `all-in-one` and `not enrolled`, posture reads `single-agent — you are the reviewer`, and it can take an item to `done` itself | ✅ (as an all-in-one key) |

---

## What I would bet breaks

Stated in advance so the walk is a test of my claims, not a search for confirmation.

**Vendor diversity has never run against real vendor strings.** My tests set
`capabilities.vendor` to `"anthropic"` and `"openai"` by hand. What a real Codex or Grok Build
client reports — if it reports anything — is unknown. If step 6 shows a same-vendor review
where a cross-vendor one was available, the cause is almost certainly the *string*, not the
preference logic.

**`host` was predicted to be the weak point — and it half resolved itself.** A real Grok
client DID report one (`vicious-apogee`), so the prediction that nothing would was wrong. Two
things then changed underneath it: GRPH-365 reversed the polarity, so an **absent** host is now
restrictive rather than permissive (the old note here said the opposite, and that note was the
bug), and PRD-19 made independence derive from the SEAT. With seats, `host` is only consulted
for un-enrolled agents — so step 13 now tests a narrower thing than it was written for.

**`wait_seconds` has never parked against a real network.** Only an injected clock. A proxy or
client timeout shorter than 60s would sever the request, and the agent would see a transport
error rather than an empty answer.

**The directive downlink has never reached a real client, and now carries two message
types.** Step 8's `role_change` and step 11's `session_expired` are equally unproven: both
assume an agent notices a `directive` field on a response it was making anyway, and acts on it.
Whether a real client's loop does depends on the prompt being followed — exactly the kind of
thing that works in principle and not in a terminal. This is still the one I would bet against.

---

## What to record

Fill in the Result column, then attach it to **GRPH-340**. For anything that fails, the useful
form is the same one this repo has used all along:

- what you did, what you expected, what happened;
- whether it is the *mechanism* being wrong or the *claim about it* being wrong — this PRD has
  now had two of the second kind (§9's subagent claim, and the invariants that read as
  universal when they describe the fleet posture only);
- and for anything I asserted above that turns out false, say so plainly — an incorrect
  prediction here is more valuable than a passing step.

If a step passes only because you did something not written down, that counts as a failure of
this guide. Note it.

---

## Results — 2026-08-13/14

Run by a human driving real Grok agents through Cursor, against the isolated `gb-fleet` stack.
**Fourteen steps recorded below. Ten defects found; all ten are now closed.** Every one was
invisible to a suite that was green throughout — 1748 passing at the time.

Eight steps (1, 2, 3, 4, 6, 6b, 8, 11) were driven by a human in four terminals on 08-13. Six
(5, 7, 9, 10, 12, 14) were scripted over the real MCP HTTP surface on 08-14 — **weaker
evidence, because they do not exercise client behaviour**, though step 9 found two defects
anyway. Step 15 is recorded in the walk table above; step 13 is superseded, seats having
replaced host as the independence discriminator.

Of the ten defects, four were fixed inside `4446fe7` during the walk and never filed; the
other six are GRPH-376, 378, 379, 395, 396 and 397, all `done`. The counts in this paragraph
were `Eight steps run. Five defects found, four fixed, one filed.` until 2026-08-22 — a
snapshot of the first day that the tables beneath it had already outgrown.

### Steps

| # | Step | Result |
| --- | --- | --- |
| 1 | Issue a wave of seats | ✅ four seats, two workers **not** deduplicated |
| 2 | Each terminal registers | ✅ roles match seats — **found both heartbeat defects** |
| 3 | A worker calls `update_item(status="done")` | ❌ **FAILED — gate unreachable** → fixed → ✅ re-run 03:39, first firing in the product's life |
| 4 | Two workers `claim_cluster` concurrently | ✅ colliding items in ONE cluster, disjoint in the other, zero shared areas |
| 6 | Reviewer signs off | ✅ **one credential, one host, one vendor** — independent by SEAT |
| 6b | Effort ≥ 3 needs a sabotage receipt | ✅ real receipt: *reverted partition to clustering.shared_touchpoints only → 1 test failed* |
| 8 | Re-task a live agent | ✅ directive rode a **heartbeat**, delivered **exactly once**, no reconnect |
| 11 | End wave | ✅ 4 seats + 2 legacy keys revoked, 2 leases + 1 reservation released, **credential survived** |
| 9 | Reviewer bounces an item | ✅ refused to a non-author twice, author took it back, **pin lapsed at the second** — and **found 2 defects** (GRPH-378/379) |
| 5 | A worker tries `claim_review` on its own item | ✅ and **stronger than the criterion** — a worker cannot call `claim_review` at all, so whose item it is never arises |
| 7 | Promote a worker to reviewer **holding its own item in review** | ✅ promotion took effect, then `claim_review` handed it somebody else's item and `sign_off` refused its own |
| 12 | Subagent registers with `parent_agent_id` | ✅ refused **while holding a reviewer seat** — a seat does not launder a call tree |
| 14 | `claim_cluster(wait_seconds=60)` on an empty backlog | ✅ **one** call, parked, returned at **8.1s** the moment work appeared |
| 10 | Kill a worker mid-lease | ✅ on the second attempt — offline **t+120s**, lease and reservation lapsed, and **two defects the step was not looking for** (GRPH-396, GRPH-397) |

Step 9 was run on 2026-08-14, after the others, and differently: **driven over the real MCP
HTTP surface by script rather than by four humans in terminals.** Three credentials, three
seats redeemed, every agent action a JSON-RPC `tools/call` against the live fleet server — the
same endpoint a Cursor terminal uses. Only the operator half (mint credential, issue wave) went
through the service layer, as the human does through the UI. Worth stating plainly: it does not
test client behaviour, so it is weaker evidence than steps 2/3/8, and it still found two
defects.

Steps 5, 7, 10, 12 and 14 were run on 2026-08-14 the same way as step 9 — scripted over the
real MCP surface. **THE WALK IS COMPLETE**: every step has an outcome, and step 13 is
superseded rather than skipped.

Step 10 needed a second attempt, and the first one is the more useful record. I reused the
agent from steps 5 and 7, whose refused calls had already tripped `QUARANTINE_AFTER_REFUSALS`
— and quarantine releases the agent's work and flags its branch. So every transition I
observed came from the wrong cause, and I would have scored the step a pass on evidence that
had nothing to do with an agent dying. The re-run used a worker that made no refused call.

One expectation in the plan is worth correcting rather than ticking. "Item back to `next` at
~600s" does not happen: the lease expires LAZILY, so the row stays `in_progress`, assigned to
a dead agent, until somebody claims it. That is by design and it is also how GRPH-397 hid —
`claim_cluster` drew its pool from `backlog`/`next` and therefore never saw it again.
Step 13 is superseded — seats decide independence now, so `host` is only the un-enrolled
fallback.

### Defects

Four of these were **never filed as items**. They were found and fixed inside one commit
during the walk — `4446fe7`, "The role gate on update_item was unreachable, and non-workers
could not stay alive" — and the walk-results commit says so plainly: *four defects found,
four fixed, one filed.*

An earlier version of this table put `GRPH-377` in the Item column for all four. That is a
different defect (ending a wave erasing authorship), so the citation pointed at real work
that was not this work — the kind of wrong reference that survives precisely because it
looks like a reference. Corrected 2026-08-21; the commit is cited instead, because that is
what exists.

| Item | What it was |
| --- | --- |
| *(not filed — fixed in `4446fe7`)* | **`update_item` never advertised `agent_id`**, so its role gate was unreachable through the published schema and a worker wrote `done`. `WORKER_STATUS_CEILING` had never gated anything in production, leaving a hole under the self-review ban. My tests passed the parameter by hand — exercising a path no client could reach — and I cited those passes as evidence repeatedly. |
| *(not filed — fixed in `4446fe7`)* | `heartbeat` was gated to `("worker",)`, so a reviewer and a planner were refused the only call that keeps them on the roster and died 150s after registering, terminals open. |
| *(not filed — fixed in `4446fe7`)* | `heartbeat` **required an item id**, so presence was maintainable only while mid-work — a planner never holds an item at all. |
| *(not filed — fixed in `4446fe7`)* | nginx set no `proxy_read_timeout`, defaulting to exactly `MAX_WAIT_SECONDS` (60s). A full-length park raced the proxy and lost about half the time; a real client hit the 504. |
| GRPH-378 | **`bounce` required a reason and discarded it.** No column held it, the event meta carried only the principal, and after a real bounce the string appeared in **no row of any table**. The author got the item back with nothing to act on — the exact failure the requirement was written to prevent. `test_a_bounce_needs_a_reason` asserts the refusal on a blank reason and stops there. |
| GRPH-379 | **The pin was invisible.** `bounce_pinned_to`/`until` and `built_by` appeared on no read surface, and a refused `claim_next` returned `{"claimed": false, "item": null}` — byte-identical to an empty backlog. A worker that should idle and retry concludes the project is finished. |
| GRPH-395 | **A review claim never expires.** `claim_review` leases by setting `reviewed_by`, and nothing ever clears it — no expiry, no sweep, and `release_item` refuses because a reviewer holds no `claimed_by`. A reviewer that dies strands the item in `review` forever, looking like ordinary queued work. Live example: `FA-9`, reviewer silent 2333s. Exactly what `bounce_pinned_until` exists to prevent, without the expiry. |
| GRPH-397 | **`claim_cluster` never re-offered abandoned work.** Its pool was `backlog`/`next`, while `claim_next` used `_is_claimable`, which counts a stale lease. An item whose holder died stays `in_progress`, so it satisfied one definition of claimable and failed the other — and once GRPH-380 made `claim_cluster` what every posture is taught, a crashed agent's item was offered to nobody at all. Two definitions of one fact, which is what most of this walk found. |
| GRPH-396 | **A dead agent's branch is never flagged.** `branch_orphaned` is written in one place — inside `quarantine()`, reachable only by an agent making three refused calls, i.e. one that is *demonstrably alive*. The agent that crashes holding a branch is recorded nowhere, and that is the common case. It also silently disables the dismissal guard built on it: Dismiss refuses on an orphaned branch, so it never refuses for the dead agents a cluttered roster is full of. |
| GRPH-376 | `sign_off` clears `claimed_by`, so **the self-review ban is unprovable after the fact** — every done item reads `built_by: -`. Enforcement is fine; the audit trail is not. **Filed as GRPH-376; fixed since — the item is `done`.** |

### What the walk proved that tests could not

- Two agents on **one credential, one host, one vendor** reviewed each other's work, independent
  purely because they redeemed different seats. That configuration was impossible before PRD-19.
- End wave revoked the wave and **left the credential authenticating** — no config touched, no
  reconnect. That is the whole point of enrolment, demonstrated rather than argued.
- A directive reached a real client on a call it was making anyway, once.
- The author pin holds on an item **nobody is holding**: after the author released it, the other
  worker was still refused. That is what makes it a reservation rather than a side effect of the
  lease — and it is the assertion the unit tests could not make, because they cannot let a lease
  age. It then lapsed on the second: refused at t+0/15/30s, claimed at t+45s against 47s left.

### Predictions, scored

Stated in advance so the walk would test claims rather than confirm them. **One of four held.**

| Prediction | Outcome |
| --- | --- |
| Vendor strings never tested against real clients | **wrong** — real clients report `xai` and `cursor` |
| `host` is self-reported and nothing reports it | **wrong** — reported, and seats made it moot |
| `wait_seconds` has never parked against a real proxy | **right** — 504 at exactly 60s |
| The directive downlink has never reached a real client | **wrong** — arrives, once, on a heartbeat |

### Environment friction — real, and not ours

Cursor's MCP client wedged mid-session and did not recover; a stale `index.html` served an old
bundle until nginx got a `Cache-Control`; the client silently drops an `mcp.json` entry it
cannot parse; and it does not interpolate `${env:VAR}` at all. More of this walk was spent
fighting the client than finding product defects, which is itself a finding for anyone standing
up a fleet.

### Method notes

Two hazards worth carrying forward. **A looping agent collects directives you meant to observe**
— the first step-8 attempt looked like a failure because one of the fleet's own calls had
already acked it. And **a green test can vouch for an unreachable path**: the `update_item`
gate had passing tests for weeks because they supplied a parameter the schema forbade. Assert
against the published surface, not the internal one.

Three more from the 2026-08-14 run, all of which produced a wrong reading before they were
caught:

- **Do not reuse one agent across steps.** The first step-10 attempt used the agent from steps
  5 and 7, whose refused calls had tripped `QUARANTINE_AFTER_REFUSALS`. Quarantine releases the
  agent's work and flags its branch — so the release and the flag I observed both came from the
  wrong cause, and would have scored step 10 as a pass on evidence that had nothing to do with
  silence. Give each step its own agent.
- **Debris makes a later step ambiguous.** By step 14 the project held live reservations from
  earlier steps, so the parked call correctly returned nothing and the step read as a failure.
  Re-run on a fresh project: the empty return then means what it says. A step that shares state
  with the steps before it is testing their leftovers.
- **`claimed: true` is not an assertion.** Step 7's first gate looked like it passed because
  `claim_review` returned success — but the question is WHICH item it returned. It had taken
  somebody else's, correctly; a bug that handed it its own would have produced the same
  `claimed: true`. Assert the id.

One method choice to state plainly: steps 5, 7, 9, 10, 12 and 14 were driven **by script over
the real MCP surface** rather than by humans in terminals. Same endpoint, same credentials,
same seats — but it does not exercise client behaviour, so it is weaker evidence than the
steps that found the heartbeat and directive defects. It still found four.
