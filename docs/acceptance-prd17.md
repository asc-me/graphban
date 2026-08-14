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
| 2 | Each terminal registers | Roster shows 4 distinct ids, correct roles, counts broken out by role |✅ roles + ids correct — **found 2 heartbeat defects** (GRPH-377) |
| 3 | A worker calls `update_item(status="done")` | `unauthorized` naming `reviewer`; item stays `review`; refusal in Activity with the human principal |❌→✅ **FAILED: gate unreachable** (GRPH-377); fixed, re-run 03:39, first firing ever |
| 4 | Two workers `claim_cluster` concurrently | Zero shared touch-areas between the held clusters |✅ two workers, zero shared areas, real concurrent claims |
| 5 | A worker tries `claim_review` on its own item | Refused — and a worker cannot call `claim_review` at all, which is stronger than the PRD's criterion |not run |
| 6 | Reviewer signs off | `reviewed_by != claimed_by`; where the fleet has the diversity, reviewer vendor ≠ author vendor |✅ one credential, one host — independent by SEAT |
| 6b | Reviewer signs off an **effort ≥ 3** item with no sabotage receipt | Refused (`conflict`), naming what is missing; passes once a receipt with `tests_failed ≥ 1` is recorded |✅ real sabotage receipt on effort 5 |
| 7 | Promote a worker to reviewer **while it holds its own item in `review`** | Both gates refuse — `claim_review` filters it out, `sign_off` asserts authorship |not run |
| 8 | Re-task a live worker → reviewer | Takes effect on its **next poll**; no reconnect; Fleet view shows the directive pending, then acked |not run — the one I still bet against |
| 9 | Reviewer bounces an item | Returns to `next`, invisible to other workers until the pin lapses (one lease), then claimable by any |not run |
| 10 | Kill a worker mid-lease | `offline` within ~150s; item back to `next` at ~600s; reservation expired; orphaned branch flagged in its row |not run |
| 11 | **End wave** | This wave's SEATS revoked; leases and reservations released; **your credential still authenticates** and the config is untouched; live agents get a `session_expired` directive on their next poll |✅ 4 seats + 2 legacy keys revoked, 2 leases released, **credential survived** |

**New since the PRD was written — worth checking too:**

| # | Step | Expected | Result |
| --- | --- | --- | --- |
| 12 | Have an orchestrator spawn a subagent that registers with `parent_agent_id` | The subagent cannot `claim_review` or `sign_off` its parent's work |not run |
| 13 | Two terminals on **one** key, same machine, both reporting `host` | Neither can review the other; the reason says to mint a per-role credential |superseded — seats decide independence now |
| 14 | A worker calls `claim_cluster(wait_seconds=60)` on an empty backlog | Parks; returns within the window when work appears; **one** tool call, not twelve |not run |
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

**Seven steps run, four defects found, all four fixed, one filed.** Every defect was invisible
to a suite that was green throughout (1748 passing at the time).

| Found | What it was |
| --- | --- |
| GRPH-377 | `update_item` never advertised `agent_id`, so the role gate was **unreachable through the published schema** — a worker wrote `done`. `WORKER_STATUS_CEILING` had never gated anything in production. My tests passed the parameter by hand, exercising a path no client could reach. |
| GRPH-377 | `heartbeat` was gated to `("worker",)`, so a reviewer and a planner were refused the only call that keeps them on the roster and died 150s after registering. |
| GRPH-377 | `heartbeat` **required an item id**, so presence was maintainable only while mid-work — a planner never holds an item at all. |
| GRPH-377 | nginx set no `proxy_read_timeout`, defaulting to exactly `MAX_WAIT_SECONDS` (60s), so a full-length park raced the proxy and lost about half the time. A real client hit the 504. |
| GRPH-376 | `sign_off` clears `claimed_by`, so **the self-review ban is unprovable after the fact** — every done item reads `built_by: -`. Enforcement is fine; the audit trail is not. Filed, not fixed. |

**What the walk proved that tests could not:**

- Two agents on **one credential, one host, one vendor** reviewed each other's work — independent
  purely because they redeemed different seats. That configuration was impossible before PRD-19.
- End wave revoked 4 seats and 2 legacy wave-tagged keys, released 2 leases and 1 reservation,
  and **left the credential authenticating**. No config was touched.
- The adversarial gate accepted a real sabotage receipt (`reverted partition to
  clustering.shared_touchpoints only → 1 test failed`) on an effort-5 item.
- The `update_item` role gate fired for the **first time in the product's life** at 03:39.

**Environment friction, recorded because it is real and not ours:** Cursor's MCP client wedged
mid-session and never recovered; a stale `index.html` served an old bundle until nginx got a
`Cache-Control`; and the client silently drops an `mcp.json` entry it cannot parse. More of this
walk was spent fighting the client than finding product defects, which is itself a finding for
anyone standing up a fleet.

**Not run:** steps 5, 7, 8, 9, 10, 12, 14. Step 8 — the directive downlink reaching a real
client — remains the one I would bet against, and now carries two message types.
