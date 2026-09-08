# Delegation — handing one item to a child on another model

**A parent hands one item to one child on a cheaper harness with two calls, and keeps
working.** The child's outcome comes back as the item changing state on the board, never as
a reply in the parent's context.

The mechanism is spread across three PRDs, because it was built in three passes:
[PRD-35](prd-35-delegation-ledger-fact.md) made a delegation a ledger fact,
[PRD-36](prd-36-delegate-to-seat.md) bound a seat to an item so the child claims it at
registration, and [PRD-37](prd-37-preference-matrix.md) made a tier resolve without an
operator flag. This document is the runbook: what to set up, in what order, and what each
refusal looks like when you hit it.

**What it is not.** Not the divvy — `claim_cluster` is still how a free worker takes work,
and an unbound seat still behaves as it always did. Not a subagent: the child is a separate
process on its own seat with no `parent_agent_id`, and lineage is the seat. Not a chat
return: `spawn` gives you an agent id at registration and nothing after it.

---

## The short version

```bash
gban login          # you, at a terminal, once. It refuses without a tty, on purpose.
gban setup          # from inside the repo: resolves the project from the directory,
                    # mints, configures both MCP servers, installs the supervisor,
                    # writes the delegation skill, and verifies all of it
gban setup --auto   # …for every project whose repository is here or beside here
```

Then restart the harness so it reads the new config, and an agent can `delegate(seat=true)`
and `spawn`.

**An agent can drive all of this.** The `graphban-delegation` skill runs `gban whoami` and
`gban setup` itself; the only thing it hands back is `! gban login`, because that one needs a
terminal. It then carries the setup the rest of the way and tells you when to restart. Everything below is what those two commands do and what to read when one of
them says no — `gban setup` is not a shortcut past understanding it, it is the same steps with
nothing left to mistype.

The split is worth stating once, because it is not arbitrary: **steps 1 and 2 are yours and
cannot be delegated** — authenticating and deciding to mint are the authority gate PRD-17 D-e
keeps on the human side. Everything after them is mechanical, which is why one command can do
it.

---

## 0. What has to be true first

**Delegate items, never fragments** (PRD-36 D10). The item needs a title, `touchpoints` and
acceptance before it can be handed over, and the touchpoints are the load-bearing part: they
become the area reservations taken against the child when it claims. An item with no
touchpoints reserves nothing and collides with everything, which is the same defect the
divvy exists to prevent, arriving through a different door.

A "look into the caching thing" item is not delegable. It gets done inline, or filed
properly first.

---

## 1. The credential — planner or all-in-one

> `gban setup` does this section, and mints the right kind. Read on if you are doing it by
> hand, or if setup refused.


Two different mechanisms sit in front of the two halves of this, and confusing them wastes
an afternoon:

- **`delegate` itself carries no role gate.** It writes what the caller asked for and claims
  nothing, and a worker fanning out to a subagent delegates as much as a planner does. It is
  in the `fleet` **tool tier**, which is what keeps it off a lone agent's manifest — and a
  tier decides what is *advertised*, never what may be *called*. So the symptom on a worker
  credential is a tool that appears not to exist, and calling it anyway works.
- **`seat: true` is planner-only**, because it mints. That gate is the real boundary, and it
  is the one this runbook needs.

**Two kinds of credential, and picking the wrong one is the mistake to avoid.**

```bash
gban setup                                     # a project credential. Does NOT expire.
gban keys mint --role planner --wave wave-1    # a WAVE credential. Expires in a day.
```

`gban keys mint` posts to `/api/fleet/keys`, which is `mint_fleet_key`, which sets
`FLEET_KEY_DAYS = 1`. That is right for a wave and wrong for standing delegation up: a
credential that dies overnight makes "delegation is enabled" quietly stop being true, and the
symptom is an agent that worked yesterday. **The seat is the object with a TTL** — thirty
minutes, single use — and that is where expiry belongs. `gban setup` mints an ordinary
project-scoped key with the same tool tiers and no expiry.

For the wave credential, `mint_fleet_key` attaches the `fleet` and `prd` tool tiers to a planner or all-in-one
credential without being asked — a planner that could not see `propose_allocation` would
fail by that tool being *absent*, which reads as it not existing rather than as a missing
grant. It attaches the `gate` scope only where a worker role is permitted, because a planner
cannot build and so has nothing to attest.

Two properties of that key are worth knowing before you mint rather than after:

- **`roles` is a ceiling, and a role change cannot climb past it.** An agent holding a key
  minted `--role worker` can never be promoted to planner; it has to be restarted on a
  different credential. If you want the option later, repeat the flag
  (`--role worker --role planner`). Narrow stays the default because a one-role key is a
  real bound — it is what stops a client config from registering a worker as a planner.
- **A fleet key lasts one day** (`FLEET_KEY_DAYS`). It is a wave credential, not an
  installation credential, and "End wave" sweeps it. This is the single most common reason a
  fleet that worked yesterday does not today.

---

## 2. The delegating process registers as an agent

`delegate` needs a registered agent — pass `agent_id`, or call `register_agent` on the same
connection first. The mint gate is checked against that agent, so the seat cannot be minted
by something that never announced itself.

---

## 3. The executor — `gbfleet`, where the children actually run

The supervisor runs on the machine that will hold the worktrees, which is not necessarily
the machine the planner is on.

```bash
uv tool install graphban-fleet      # gives you gbfleet and gbagent
gbfleet doctor --server <url> --project <project> --adapter <vendor>
```

Run `doctor` **before** the first spawn. It answers the questions that otherwise cost a
whole wave: does this repository commit a seat path, can a credential file be kept private
on this filesystem, is the vendor binary inside the supported range, does the server accept
this key, does the preference matrix load. It reports PASS, FAIL and **UNKNOWN** separately,
because a check that could not run is not a check that passed.

---

## 4. Attach the supervisor to the planner

The planner ends up holding two MCP servers, and only one of them has authority:

```
planner
 ├─ graphban   (remote HTTP)   → delegate, mint_enrolment, fleet_status, the ledger
 └─ gbfleet    (local stdio)   → spawn, stop, ps, orphans
```

```bash
gbfleet mcp --repo . --server <url> --project <project> \
            [--tier cheap=gbagent:<model>] [--tier frontier=claude:opus]
```

- **`--project` matters when the credential spans several projects.** It is named on every
  call so the spawn lands where the seats were minted.
- **`--tier` is optional.** A flag, when present, *is* the resolution, and the spawn reply
  says `source: flag`. Without one, the tier resolves through the shipped preference matrix
  — harness × model × lane × tier, filtered by what is installed on this machine, your
  profile and your policy — and the reply explains what it dropped and why. Use a flag to
  pin a machine; leave it off to get the matrix's answer.
- **The table is fixed for the life of the process.** Changing a model is a restart. Every
  spawn reply names the adapter and model that actually ran, so a stale mapping shows up on
  the first child rather than on a bill.
- **One supervisor per repository**, by lock. A second one refuses and names the pid holding
  it; it never blocks and never waits.

---

## 5. The two calls

```
delegate(id="GRPH-x", lane="backend", tier="cheap", seat=true)
  → { delegation_id, state, withdrew, enrolment_code, brief }

spawn(enrolment_code="…", tier="cheap", item="GRPH-x", wave="w1")
  → { agent_id }
```

`lane` is one of `frontend`, `backend`, `mixed`; `tier` is `cheap` or `frontier`. **Both are
required and neither has a default.** `get_item_details` carries a `brief` that suggests
them with its basis, and you type the value anyway — the server never guesses what you are
willing to pay for. Paste `brief.text` into the spawn.

What `seat: true` buys: the delegation also mints a **worker seat bound to the item**.
`register_agent` on that seat resolves the item, claims it, reserves its touch areas and
links the delegation, all inside one savepoint — so a claim with no link, or a link with no
claim, cannot exist. Registration itself commits regardless. The child therefore **holds the
item before its first tool call**, and the delegation reads `claimed by <child>` immediately
rather than eventually.

The reply's `assigned` is the authority on what the child got:

| `assigned.state` | Meaning |
| --- | --- |
| `claimed` | The seat handed it this item, with its areas reserved. |
| `taken` | Someone else holds it, or it is no longer claimable — with `reason` and `held_by`. The child should exit. Registration is not refused, because the child has to exist to be told. |
| `none` | An unbound seat, or no seat. Today's behaviour. |

### Adapter arguments you cannot omit

- **gbagent requires `turns` and `window`.** The adapter refuses to guess either, so a
  gbagent spawn without them exits *before registering* — which presents as a delegation
  that expired with nothing claimed, not as an error on the spawn.
- `fallback_model` is claude-only; `effort` is grok-only; `debug` works for grok and claude,
  and the reply says so for the adapters that have no such flag rather than leaving you to
  assume it worked.

---

## 6. Reading the outcome

Nothing is pushed to the parent. Read the item, the Live board, or `fleet_status`. That is
the point of the arrangement: the parent's context grows by two tool results rather than by
a child's transcript.

Three timers run independently, and none inherits from another:

| Timer | Length | What it decides |
| --- | --- | --- |
| Delegation lease | 600s | `open` versus `expired`. A late child still links and reads `claimed`. |
| Item lease | 600s, fresh at the claim | Whether the item looks abandoned. |
| Seat TTL | 30 min | How late a child may arrive at all. |

**`expired, nothing claimed` is a real state, not a gap.** It is the spawn that died before
it registered — which nothing in the product could show before PRD-35.

---

## 7. You may not sign off your own delegation

`sign_off` and `claim_review` refuse a reviewer that is the delegation's `delegated_by`:

> `<agent> delegated <item> to <builder> and cannot sign it off; another agent has to review
> a delegated item`

It is refused **on the delegation record, not on parentage**, so no role change launders it.
The delegator chose the item, wrote the brief and picked the tier — that is co-authorship of
the plan, and review exists to be a second opinion. A **bounce is still allowed**, because
rejecting is not approving.

Budget a second agent for review, or the wave stalls at `review` with nobody entitled to
clear it.

---

## 8. The refusals, and what each looks like

| Symptom | What it means |
| --- | --- |
| `delegate` is not in the manifest | The credential lacks the `fleet` tool tier. That is advertisement, not permission — the call still works. Mint a planner or all-in-one key to see it. |
| `mint_enrolment requires role 'planner'; <who> is registered as 'worker'` | The minting half is planner-gated even though `delegate` is not. |
| `<item> is blocked: <blocker>` | Clear the blocker first. |
| `you hold <item>; release it or build it yourself` | A delegation is for work you are *not* holding. |
| `<item> is not ready: status <s>` | Only a claimable item can be delegated. |
| `<item> is pinned to its author after a bounce` | The author gets first retry. Wait for the pin to lapse. |
| `<item> already has an open delegation from <agent>, <n>s old` | Only its owner can withdraw it; anyone else waits for it to expire. Re-delegating your **own** withdraws it silently and reports `withdrew`. |
| `<item>'s areas are reserved by <agents>` | A bound seat would claim straight through a collision the divvy would have caught. **Nothing is written — not even the delegation row.** It deliberately does not fall back to an unbound seat: a binding that quietly downgrades is the failure the binding exists to prevent. |
| `tier 'x' is not mapped on this supervisor` | Only fires when `--tier` flags were given and this tier is not among them. Without flags it falls through to the matrix. |
| `no harness resolves for tier 'x'` | The matrix dropped every row; the error carries the dropped rows and why. |
| `already <n> live worker(s); max_workers is <n>` | Stop one or wait. |
| `assigned.state: "taken"` | Someone beat the child to it. The delegation was superseded or already finished. |
| Delegation reads `expired, nothing claimed` | The child never registered. Check the adapter arguments first — see gbagent's `turns`/`window` above. |

---

## 9. A whole backlog is a different tool

Per-item `delegate` + `spawn` is for work you have already chosen to hand over. For a
backlog, use the loop:

```bash
gbfleet until --repo . --server <url> --project <project> \
              --adapter <vendor> --request cheap --max-children 8
```

It mints bound seats just in time, spawns, watches and stops on **genuine idle** — no ready
work, no unsigned review, and no live lease — rather than merely on the last child exiting.
There is no LLM inside it. Fanning the same wave out by hand keeps a frontier context awake
for its entire length, polling and adjudicating bounces.

---

## What this runbook cannot tell you

It describes the mechanism as the code implements it. It cannot tell you **whether a given
item is worth delegating** — the brief suggests a lane and a tier with its basis, and the
decision stays with the parent, on purpose. And it cannot tell you that your matrix rows
describe models you actually have; that is `gbfleet doctor`'s job, and it is the check most
worth running before a wave rather than after one.

## See also

- [`fleet/README.md`](../fleet/README.md) — the supervisor itself, and why it holds no authority
- [Fleet adapters](fleet-adapters.md) — the vendor CLIs a child can run
- [Does the fleet actually work?](fleet-supervisor-walk.md) — the acceptance walk against a real server
- [MCP tools](mcp.md) — `delegate`, `mint_enrolment`, `register_agent` in the tool table
- [Data model](data-model.md) — the `delegations` and `enrolments` tables
