# PRD-19 — Enrolment sessions: one credential, ephemeral roles

**Status:** draft
**Depends on:** PRD-17 (fleet roles) · GRPH-362 (all-in-one posture) · GRPH-365 (declared independence)

## 1. Overview

MCP gives a client exactly **one static header per server entry**. Today that single string
carries four different things at once:

| Carried by the key | Should change… |
| --- | --- |
| **Identity** — which user, which project | rarely |
| **Authorization ceiling** — read/write scopes | rarely |
| **Role** — planner / worker / reviewer | per run |
| **Wave membership** — which cohort, for End wave | per wave |

Two of those are long-lived and two are ephemeral, and they are welded together. Every
difficulty in the PRD-17 rollout traces back to that weld rather than to any of the individual
features.

This PRD splits them:

- **Credential** — who you are and your ceiling. Long-lived, set once in a client config,
  ideally usable across the whole platform.
- **Enrolment session** — what role you hold, for this run. Ephemeral, established at runtime,
  issued by the human, consumed once.

The session already half-exists: `register_agent` returns an `agent_id` and every subsequent
call carries it. What is missing is that on a shared credential the **role is self-declared**,
so it is advisory rather than enforced. Enrolment codes close exactly that gap.

## 2. Goals

- **G1** — A credential is written into a client config **once, ever**. No per-wave rewrite.
- **G2** — Roles are **enforced** even when every agent shares one credential, because the
  server issued the grant rather than the agent asserting it.
- **G3** — A client that stores one MCP config for all its agents (Cursor) runs a full fleet
  with no workaround, no per-worktree files, and no plugin credential problem.
- **G4** — Two agents on one credential are independent **by construction**, because they
  consumed different enrolments — not because they remembered to declare an attribute.
- **G5** — The default costs nothing: an agent with no enrolment is `all-in-one`, the
  single-dev posture, which is what most installs are.
- **G6** — Nothing that works today stops working. Role-narrowed credentials remain valid.

## 3. Problem

**Measured, not asserted.** Every item here was observed during the PRD-17 acceptance walk, 2026-08-12/13.

**A per-role credential has nowhere to live in Cursor.** Cursor stores one `mcp.json` and
reuses it across every agent. There is no per-agent scoping — confirmed in its docs.

**Environment indirection does not rescue it.** Probed against Cursor 3.16.2 with the variables
provably present in the process (`ps eww` after `launchctl setenv`), one credential per
hypothesis so `last_used` identified which resolved: `${env:VAR}`, `${VAR}` and `$VAR` were all
ignored in both `url` and `headers`. nginx logged only the control's handshake — **no 401s**,
so the entry is silently *dropped* rather than sent as a literal. A config that looks correct
and connects nothing.

**So the config carries literal keys and is regenerated per wave.** That is the current state
after GRPH-365: one paste per wave, and the file holds three credentials.

**`instance` exists only to paper over the weld.** Because a shared credential cannot
distinguish sessions, GRPH-365 added a self-declared tag so two agents could be told apart.
It works, and it is honest about being advisory — but it is a workaround for the credential
carrying a thing it should not.

**Nine tests broke when independence was tightened**, every one a shared-credential fleet
declaring nothing. That is a signal about the model, not about the tests: the natural way to
stand up a fleet collides with the only mechanism that made review meaningful.

## 4. Key decisions

### D-a. The credential stops carrying role and wave

A credential carries identity, project scope and read/write ceiling. It does **not** carry a
role. `api_keys.roles` remains for back-compat and keeps behaving as a ceiling — it may still
*narrow* what an enrolment can grant — but the recommended path stops using it to assign.

### D-b. Enrolment codes are issued by the human and consumed by the agent

The Fleet view issues short-lived, single-use codes — **one per agent slot, not one per
role**:

```
wave-1     PLANNER-7F3K    unused    expires 14:32
           WORKER-Q28M     unused    expires 14:32
           WORKER-B4XT     unused    expires 14:32
           REVIEWER-M91C   unused    expires 14:32
```

Two workers need two codes. That falls out of D-d rather than being a preference: if
independence derives from the enrolment, two agents sharing one code share an enrolment and
are **not** independent — a reviewer could not review a worker that enrolled on the same code.
A code is therefore a seat, not a role.

The agent calls `register_agent(enrolment_code="WORKER-7F3K")`. The server consumes the code,
records the session, and returns the granted role. A worker cannot promote itself, because it
has nothing to promote itself *with*.

**A code is not a credential**, and that distinction is the point. It grants one role on one
project for one session, and it expires. Pasting it into a prompt — which is what a human will
do — leaks a role for minutes, not an API key. That makes "put it in the prompt" safe, which is
what people wanted to do anyway.

### D-c. No enrolment means `all-in-one`

An agent that registers without a code gets the single-agent posture: unrestricted, no gate,
the human is the reviewer. This is already the GRPH-362 rule for a `posture: single`
credential, generalised — and it means the common install needs no codes at all.

### D-d. Independence derives from the session

Two agents that consumed different enrolments are different sessions and therefore
independent, with nothing self-declared involved. The GRPH-365 discriminators
(`instance`/`worktree`/`host`/`vendor`) remain as the fallback for un-enrolled agents sharing a
credential, and stay exactly as strict — absence is still not a difference.

### D-e. End wave revokes sessions, not credentials

Today End wave revokes the wave's keys, which is why keys have to be per-wave. With enrolment,
it expires the wave's **sessions** and releases what they hold. The long-lived credential is
untouched, so the client config keeps working and the next wave is a new set of codes.

### D-f. A ceiling conflict is refused, never quietly narrowed

If a `reviewer` code is presented on a credential eligible only for `worker`, registration
fails with `unauthorized` naming both sides. Clamping to `worker` would leave the roster
showing a worker where the human deliberately issued a reviewer — the silent-downgrade shape
this repo keeps producing, and the one thing an operator cannot debug from the UI.

### D-g. A planner may mint enrolments — with two traps that are easy to walk into

An orchestrator that spawns its own agents cannot paste a code from a UI, so `mint_enrolment`
is a tool, bounded by the minting credential's ceiling.

**Trap 1: it must be planner-only, and that is what contains the laundering vector.** If a
worker could mint, it could build an item, mint itself a reviewer seat, register as a new agent
— new id, new enrolment, therefore independent — and sign off its own work. The authorship ban
is keyed on agent id and would not see it. Planners are already refused `claim_next` by the D2
role gate, so a planner has **no authored work to launder**. The capability is safe precisely
because the role that holds it cannot build.

**Trap 2: a minted enrolment must NOT set `parent_agent_id`.** The intuitive move is to record
the minter as the parent, and it would break the whole feature: `independent()` already treats
*siblings under one parent* as one call tree, so every seat a planner issued would be mutually
non-independent and no agent in an autonomously provisioned fleet could review any other. The
enrolment records `minted_by` for audit; parentage stays for what it means — a subagent
declaring it runs inside another agent's process.

## 5. Data model

```
enrolments
  id                 pk
  project_id         fk
  code_hash          sha-256; the plaintext is shown once, like a key
  role               planner | worker | reviewer | all-in-one
  minted_by          agent id, null when a human issued it (D-g audit trail)
  wave               text, for End wave
  issued_by          user id
  expires_at         default now + 30 min
  consumed_at        null until used — single-use, and NOT configurable (O1)
  consumed_by        agent id, for the audit trail a reissue leaves behind
  reissued_from      self-fk, nullable — links a replacement seat to the dead one
  revoked            bool

agents
  enrolment_id       fk, nullable — NULL means the un-enrolled default (all-in-one)
```

There is deliberately **no `max_uses`**. It reads as a harmless convenience and is the same
independence bug as sharing a credential: two sessions on one enrolment cannot review each
other. Single-use is a correctness property here, not a security preference.

No change to `api_keys` beyond what GRPH-362 added.

## 6. Rollout

Each slice below is its own `##` section, because that is the atom `decompose_prd` tracks — a
single "Slices" heading collapses eight pieces of work into one item that can never be
partially done. They are ordered; E8 deletes and must run last.

## E1 — The enrolments table and the issue/consume service

`enrolments` per §5, with TTL and **single use** enforced in the service rather than by
convention. **Reissue** mints a replacement seat carrying `reissued_from`, so a crashed agent
is recovered by issuing a new seat and the dead one survives as the audit record (O1/A4).
No `max_uses`: two sessions on one enrolment cannot review each other.

## E2 — register_agent consumes an enrolment code

`register_agent(enrolment_code=…)` looks the code up, checks it against the credential's
ceiling **before consuming it** — so a refusal does not burn the seat — and grants the role.
A ceiling conflict is refused with `unauthorized` naming both sides, never clamped (D-f/A8).
No code at all yields `all-in-one` with `enrolled: false` in the response (D-c/A2).

## E3 — Independence derives from the session

If both agents carry an `enrolment_id` and the ids differ, they are independent. Otherwise
fall through to the GRPH-365 discriminators, unchanged and exactly as strict — absence is
still not a difference (D-d/A6). A minted enrolment must NOT set `parent_agent_id`: the
sibling rule would otherwise collapse an autonomously provisioned fleet into one call tree
(D-g, trap 2).

## E4 — The Fleet view issues and tracks seats

Issue a wave of seats — one per agent, not one per role — and show each as unused, consumed or
expired, with **Reissue** on a dead one. The roster renders `all-in-one · not enrolled` rather
than a bare role badge, so a forgotten code is visible rather than a silent downgrade (O3/A2).

## E5 — End wave expires sessions, not credentials

Expire the wave's sessions and release every lease and reservation they hold. The credential is
untouched, so the client config keeps authenticating and the next wave is a new set of seats
(D-e). Expiry reaches a live agent as a `session_expired` directive on its next poll, over the
EXISTING downlink — no new transport (A3).

## E6 — Role prompts carry the seat

The generated fleet prompts instruct `register_agent(enrolment_code=…)` first, and the Fleet
view's copy button substitutes the seat's code so a human pastes a filled prompt rather than
editing one (A5). Covered by the existing prompt-vs-manifest tests.

## E7 — mint_enrolment, planner-only

A planner may mint seats for agents it spawns, bounded by its own credential's ceiling, with
`minted_by` recorded. **Planner-only is the containment, not an extra check**: a worker that
could mint would build an item, mint itself a reviewer seat, register as a new agent, and sign
off its own work past an authorship ban keyed on agent id. Planners are already refused
`claim_next`, so they have no authored work to launder (D-g, trap 1).

## E8 — Delete what enrolment makes dead

Last, and deliberately, so nothing is removed before its replacement is live: the Fleet view's
wave config emission, `web/src/features/fleet/wave.ts`, the per-wave key minting path, and the
plugin README's wave instructions. **`instance` and the GRPH-365 discriminators stay** — they
are the fallback for un-enrolled agents sharing one credential (D-d).

## 7. Non-goals

- **Not an auth system.** A code is a role grant inside an already-authenticated call. It never
  replaces the credential.
- **Not an adversarial boundary.** An agent that holds a reviewer code and a worker code can
  still cross roles. Enrolment makes the *accident* impossible; it does not defeat intent.
- **Not OAuth.** Per-agent authentication is a different, larger change.

## 8. Risks and open questions

**O1 — Restart mid-wave.** *Resolved by the seat model, in a way the first draft got wrong.*
Raising `max_uses` was the obvious fix and is unavailable: two sessions on one code are not
independent, so a reused code silently disables review between them. Binding reuse to an
`agent_id` does not help either — a restarted process has lost the id it would present. So a
code stays single-use, and the remedy is **reissue**: one click per dead seat, which is also
the only action that leaves an audit trail of what happened.

**O2 — Codes in transcripts.** *Resolved: 30 minutes, and no binding.* A code pasted into a
prompt lands in logs and history; TTL plus single-use bounds the damage to one role on one
project for half an hour, and the credential's ceiling still applies on every call after that.

Binding redemption to a credential or an IP was considered and rejected: single-use ALREADY
bounds a code to exactly one redemption, so binding adds friction — the seat stops working if
the agent connects from a second machine — without adding a property. Two mechanisms enforcing
one fact is the shape A7 rejected for ceilings, and it is the same answer here.

**O3 — Silent downgrade.** If a human forgets to paste a code, the agent silently becomes
`all-in-one` — safe, but not what they intended, and the fleet quietly stops being a fleet. The
roster must show `enrolled: no` prominently rather than just a role badge.

**O4 — Autonomous fleets.** *Resolved: yes, planners may mint — see D-g for the two traps.*

**O6 — Enrolment defeats the role-gated manifest.** *Found while building E2, not predicted
here.* The manifest is trimmed per the KEY's eligible roles, which works because the Fleet
view mints single-role credentials. Under enrolment the recommended setup is ONE UNRESTRICTED
credential — so the ceiling is all three roles, nothing trims, and every agent pays the full
manifest again. A single-role key currently saves 16-19%.

Trimming per ENROLMENT would restore it, and cannot be done on this transport: the enrolment
is unknown until `register_agent` has run, while `tools/list` is fetched once at connect. That
is the SSE change PRD-17 D-b ruled a non-goal. Not a blocker for E1-E8 — it is a token cost,
not a correctness one — but it should be decided before the manifest is trimmed further on the
assumption that role gating still pays.

**O5 — Does this supersede the wave config?** *Resolved: delete what enrolment makes dead,
in E8, and not before.* Named explicitly there so the removal is a reviewable act rather than
code that quietly stops being reachable. The probe result and the independence-polarity fix
from GRPH-365 are NOT dead and stay.

## 9. Success criteria

1. One credential in `~/.cursor/mcp.json`, never rewritten, and three Cursor agents hold three
   different roles — verified by the roster, not by their prompts.
2. A worker that tries `sign_off` is refused, on a shared credential, with no `instance`
   declared anywhere.
3. A reviewer signs off a worker's item; both consumed different codes on the same credential.
4. An agent registering with no code is `all-in-one` and can take an item to `done` itself.
5. A code consumed twice is refused the second time; an expired code is refused.
6. End wave: sessions expire, leases release, and the client config still authenticates.
7. A code cannot grant a role the credential's ceiling forbids, and the refusal does NOT
   consume the seat.
8. A planner mints a seat, a subagent consumes it, and that subagent can review work built by
   another of the planner's seats — the sibling rule must not collapse an autonomously
   provisioned fleet into one call tree (D-g, trap 2).
9. A worker cannot call `mint_enrolment` at all (D-g, trap 1).


## 10. Appendix

**Grill, round 1.** Answers to the questions `grill_prd` raised. Three of them found real errors, which are folded
into §4 and §6 above rather than left here.

**A1 — How the granted role reaches the agent.** `register_agent` already returns
`active_role` and `eligible_roles`; enrolment adds `enrolled: true`, the granting `role`, and
`enrolment_expires_at`. Additive only, so a client written against PRD-17 keeps working and
simply never sees `enrolled: true`.

**A2 — Telling "unenrolled" from "forgot to enrol".** The server cannot, and should not
pretend to: it can only report the fact. `register_agent` returns `enrolled: false` alongside
a plain sentence saying this is the single-agent posture and no role gate applies, and the
roster renders `all-in-one · not enrolled` rather than a bare role badge. The distinction
between deliberate and forgotten belongs to the human, and the UI's job is to make the state
impossible to miss — which is exactly what O3 is about.

**A3 — How a client learns its session ended.** It rides the **existing** directive downlink
(PRD-17 D6): `role_assigned_at > role_acked_at` is already the outbox, so an expired session
arrives as a `directive` on whatever the agent polls next, with `type: "session_expired"` and
a machine-readable next step. No new transport, no push, no SSE. Role-gated calls made after
expiry are refused with `unauthorized` and a hint to re-enrol — the refusal is the backstop for
an agent that ignores the directive, exactly as D6 already works.

**A4 — Restart within the TTL.** See O1: single-use is not negotiable because a shared code
collapses independence, so the answer is reissue rather than reuse. The Fleet view shows a
consumed seat and offers **Reissue**, which mints a new code for that seat and leaves the old
row as a consumed record. That record IS the audit trail.

**A5 — Prompt placeholder.** The generated role prompt carries one line:

```
Call register_agent(enrolment_code="<PASTE THE CODE FROM THE FLEET VIEW>",
                    label="<model> @ <host>", worktree=..., branch=...) FIRST.
```

The Fleet view's copy button substitutes the seat's code, so the human pastes a filled prompt
rather than editing one. There is no separate machine path in v1 — that is O4.

**A6 — Mixed enrolled and unenrolled agents on one credential.** The check is ordered and
needs no new state: if **both** agents carry an `enrolment_id` and the ids differ, they are
independent. Otherwise fall through to the GRPH-365 discriminators unchanged. One nullable
column on a row already being loaded; no measurable cost.

**A7 — A ceiling reduced after a session started.** The session is not revoked. Ceilings are
evaluated at CALL time by `role_for_call`, so narrowing a credential's `roles` takes effect on
the very next call and the agent is refused from that moment. Revoking sessions on a ceiling
edit would add a second enforcement point for one fact, and the existing one already fires.

**A8 — Enforcing "a code cannot exceed the ceiling".** An explicit check in `register_agent`
before the code is consumed — so a refused registration does **not** burn the seat. Returns
`unauthorized`: *"this credential is eligible for worker; the code grants reviewer"*, with a
hint to mint a credential for that role or issue a worker code. Refused rather than clamped,
per D-f.

### What the grill changed

- **Codes are seats, not roles** (D-b). Two workers need two codes, because two agents sharing
  an enrolment are not independent — which would have silently disabled review inside a wave.
- **Ceiling conflicts refuse rather than clamp** (D-f). Clamping would show a worker where a
  reviewer was issued.
- **O1 was resolved by discovering the obvious fix is unavailable.** `max_uses > 1` reads as a
  convenience and is actually the same independence bug as reusing a credential.
