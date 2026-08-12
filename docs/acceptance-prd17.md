# PRD-17 acceptance walk — a real fleet, four terminals

D1–D9 are shipped and tested. **Everything they assert was verified by tests I wrote against
code I wrote.** This walk is the first time a human drives it, and this repo's history says
that is where the real defects are: the hosted instance that sat live and unreachable for
twelve days, the password button that did not exist, the fork-detection test that passed with
its matching entirely broken.

So run it looking for the things a test structurally cannot see, not to tick boxes.

---

## Setup

Use an **isolated compose project**, same reason as the PRD-14/15 walks: the compose project
name is pinned to `agentledger`, so on a machine with development history `start.sh` would
attach to the real `agentledger_agentledger_pgdata` volume and the walk would run against
live data.

```bash
COMPOSE="docker compose -p gb-fleet" \
  DB_PORT=5456 API_PORT=8012 WEB_PORT=8092 \
  PROJECT_NAME="Fleet Acceptance" ./start.sh
```

Note the printed operator password and API key — shown once. Fleet view: `http://localhost:8092/fleet`.

> `ubuntu-srv` is also on this revision (`561d99e`, alembic `0064`) if you would rather use
> the LAN box: `http://192.168.50.81:8080/fleet`. It has real project data, so prefer the
> isolated stack unless you want the walk to exercise a populated backlog.

You need **four terminals**, ideally different vendors — the point of step 6 is cross-vendor
review, and an all-Claude fleet cannot demonstrate it. Two is enough for steps 1–9 if that is
what you have; note which you ran with.

---

## Three things that will look like bugs and are not

**1. A hand-minted key produces an `all-in-one` agent, not a worker.** An unnarrowed
credential means the single-agent posture, where you are the reviewer and no gate applies. So
**step 3 will not refuse anything if you use your normal key** — you must mint from the Fleet
view, which narrows `roles` to one. If a worker is not being refused `done`, check its
credential before assuming the gate is broken.

**2. `claimed: false` is usually the correct answer.** From `claim_review` it means nothing is
waiting *that you may take*; from `claim_cluster` it means every ready cluster collides with
work in flight. Both carry a `reason` — read it. Neither is an error.

**3. Presence and item-reclaim run on different clocks.** An agent reads `offline` after
`lease_seconds / 4` (150s at the default), but its item stays held until the full lease
(600s). Step 10 needs ~2.5 min for the first and ~10 for the second. That gap is deliberate:
show the death at once, keep the half-finished work reserved in case the process is merely
wedged.

---

## The walk

| # | Step | Expected | Result |
| --- | --- | --- | --- |
| 1 | Onboard 4 agents from the Fleet view | 4 keys, each single-role, 24h expiry, wave-tagged | |
| 2 | Each terminal registers | Roster shows 4 distinct ids, correct roles, counts broken out by role | |
| 3 | A worker calls `update_item(status="done")` | `unauthorized` naming `reviewer`; item stays `review`; refusal in Activity with the human principal | |
| 4 | Two workers `claim_cluster` concurrently | Zero shared touch-areas between the held clusters | |
| 5 | A worker tries `claim_review` on its own item | Refused — and a worker cannot call `claim_review` at all, which is stronger than the PRD's criterion | |
| 6 | Reviewer signs off | `reviewed_by != claimed_by`; where the fleet has the diversity, reviewer vendor ≠ author vendor | |
| 6b | Reviewer signs off an **effort ≥ 3** item with no sabotage receipt | Refused (`conflict`), naming what is missing; passes once a receipt with `tests_failed ≥ 1` is recorded | |
| 7 | Promote a worker to reviewer **while it holds its own item in `review`** | Both gates refuse — `claim_review` filters it out, `sign_off` asserts authorship | |
| 8 | Re-task a live worker → reviewer | Takes effect on its **next poll**; no reconnect; Fleet view shows the directive pending, then acked | |
| 9 | Reviewer bounces an item | Returns to `next`, invisible to other workers until the pin lapses (one lease), then claimable by any | |
| 10 | Kill a worker mid-lease | `offline` within ~150s; item back to `next` at ~600s; reservation expired; orphaned branch flagged in its row | |
| 11 | **End wave** | This wave's keys revoked; leases and reservations released; **a hand-minted key untouched** | |

**New since the PRD was written — worth checking too:**

| # | Step | Expected | Result |
| --- | --- | --- | --- |
| 12 | Have an orchestrator spawn a subagent that registers with `parent_agent_id` | The subagent cannot `claim_review` or `sign_off` its parent's work | |
| 13 | Two terminals on **one** key, same machine, both reporting `host` | Neither can review the other; the reason says to mint a per-role credential | |
| 14 | A worker calls `claim_cluster(wait_seconds=60)` on an empty backlog | Parks; returns within the window when work appears; **one** tool call, not twelve | |

---

## What I would bet breaks

Stated in advance so the walk is a test of my claims, not a search for confirmation.

**Vendor diversity has never run against real vendor strings.** My tests set
`capabilities.vendor` to `"anthropic"` and `"openai"` by hand. What a real Codex or Grok Build
client reports — if it reports anything — is unknown. If step 6 shows a same-vendor review
where a cross-vendor one was available, the cause is almost certainly the *string*, not the
preference logic.

**`host` is self-reported and nothing has ever reported it.** The independence rule in steps
12–13 depends on it, and D8's prompts ask for it — but a human pasting a prompt may not
substitute the placeholder. If step 13 permits a review it should refuse, check whether both
agents actually sent a `host`. An absent host deliberately does **not** count as a match.

**`wait_seconds` has never parked against a real network.** Only an injected clock. A proxy or
client timeout shorter than 60s would sever the request, and the agent would see a transport
error rather than an empty answer.

**The directive downlink has never reached a real client.** Step 8 assumes an agent notices a
`directive` field on a response it was making anyway. Whether a real client's loop *acts* on
it depends on the prompt being followed, which is exactly the kind of thing that works in
principle and not in a terminal.

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
