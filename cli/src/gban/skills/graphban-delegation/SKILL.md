---
name: graphban-delegation
description: Hand a Graphban work item to a child agent running on a cheaper model, and enable delegation on a project that does not have it yet. Use when asked to delegate, fan out, spawn a worker, or run a wave.
---

# Delegating a Graphban item

You hand **one item to one child** and keep working. The outcome arrives as the item changing
state in the ledger — never as a reply in your context. Two calls do it: `delegate` then
`spawn`.

Do not restate this procedure to the user. Run it.

## 1. Check whether you can delegate at all

Call `get_context`.

- `delegate` is in your tools and `missing_tiers` does not list `fleet` → go to step 3.
- otherwise → your credential does not **advertise** the delegation tools. Go to step 2.

A missing tool tier is not a refusal. It decides what is *listed*, never what may be called,
so the symptom is a tool that appears not to exist. Read `missing_tiers` rather than concluding
the feature is absent.

## 2. Enabling it — you do all of it except the two lines a person must type

**Run this yourself:**

```bash
gban whoami
```

**If it reports a session**, keep going without asking. From inside the repository the work
belongs to, run:

```bash
gban setup
```

`setup` works out the project from **the directory you are in**, matched against the projects
the deployment says the person can read. It does not fall back to the default `gban login`
stored — logging in once inside one project must not silently mint a credential for it while
you stand in another repository. If it says `unable to resolve a project`, it lists what it
could have matched: pick from that list and re-run with `--project <id>`. Do not guess, and do
not pass the stored default just because the error mentions it — the error mentions it to
explain why it was refused.

That mints a project-scoped credential that does not expire, writes the `graphban` and
`gbfleet` MCP entries where the harness will actually read them, installs the delegation
skill, and verifies the result. Read its output: every line is `PASS`, `FAIL` or `UNKNOWN`,
and `UNKNOWN` means a check could not run, not that it passed.

**If it reports no session**, that is the one thing you cannot do. Give the person the command
to run, in exactly this form, and stop:

> Run this and tell me when it is done:
>
>     ! gban login

The `!` prefix runs it in this session, so the output comes back to you and you can carry on
from there. `gban login` needs a real terminal: without a tty the prompt cannot turn off echo,
so the CLI refuses rather than write a password into the scrollback. **Do not** try to work
around it — no piping a password in, no password in a command, no hunting for one in the
environment or a file. Wait, then resume at `gban setup` yourself.

`gban setup` installs the supervisor itself — typing the command is the consent, so it does
not stop to ask. If it reports `UNKNOWN supervisor`, read the reason it gives: **uv missing**
and **installed but not on PATH** are different problems with different fixes, and only the
first one needs a person:

> Only if setup said uv is missing:
>
>     ! uv tool install graphban-fleet

Delegation records perfectly well without a supervisor. It is what runs the child on this
machine, so a missing one is not a failed setup.

**Several projects at once.** If the person has more than one project and their repositories
sit side by side, `gban setup --auto` does all of them: it matches this directory, what is in
it, and its siblings against their project names. A project carries no repository link, so
that is a match and not a lookup — it refuses every ambiguity rather than guessing, and names
each project it could not place. Read those lines out; they are the ones needing a decision.

**When setup passes, ask for a restart and stop.** MCP servers are read at startup, so the
tools cannot appear in the session that configured them, however correct the config is. Say
that plainly — do not call `get_context` again hoping it changed, and do not retry `gban
setup`. It succeeded.

## 3. Delegate

```
delegate(id="<item>", lane="frontend|backend|mixed", tier="cheap|frontier", seat=true)
  → { enrolment_code, brief, delegation_id }
```

`lane` and `tier` are required and have no default. `get_item_details` carries a `brief` that
suggests both with its basis — read it, then choose. The server will not guess what you are
willing to pay for.

`seat=true` mints a worker seat **bound to the item**, which is what makes the child claim it
at registration rather than racing for it.

Delegate whole items, never fragments. If it has no title, touchpoints and acceptance, it is
not ready to hand over — the touchpoints become the area reservations that stop the child
colliding with other work. File it properly or do it inline.

Common refusals, none of which are bugs: the item is blocked; you are holding it yourself;
someone else has an open delegation on it; its areas are reserved by another agent, in which
case **nothing is written at all** and you should pick a different item.

## 4. Spawn

```
spawn(enrolment_code="…", tier="cheap", item="<item>", wave="<name>")
  → { agent_id }
```

Paste `brief.text` from step 3 into the child's instructions.

If the adapter is `gbagent`, `turns` and `window` are **required** — it refuses to guess them,
and a spawn without them exits before registering. That presents as a delegation that expired
with nothing claimed, not as an error on the spawn, so check these first when a child never
appears.

## 5. Read the outcome from the ledger

Nothing is pushed back to you. Read the item, or `fleet_status`. `spawn` returns an agent id at
registration and nothing after it — that is the design, not a gap. Your context grows by two
tool results instead of by a child's transcript.

Three independent timers: the delegation lease (600s) decides open versus expired, the item
lease starts fresh at the claim, and the seat expires after 30 minutes. `expired, nothing
claimed` is a real state — it means the child never registered.

## 6. You may not sign off your own delegation

`sign_off` and `claim_review` refuse a reviewer who is the delegation's `delegated_by`. You
chose the item, wrote the brief and picked the tier; review exists to be a second opinion. A
**bounce is still allowed**, because rejecting is not approving.

So a delegated item needs a second agent to review it. Plan for that, or the work stops at
`review` with nobody entitled to clear it.

## When not to use this

For a whole backlog, ask the person to run `gbfleet until` instead. It runs the wave with no
model in the loop; fanning the same work out by hand keeps your context awake for the wave's
entire length just to poll and adjudicate.
