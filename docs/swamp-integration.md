# Running Graphban with Swamp

> **Status: the port is built and armed.** The `gate` scope (GRPH-541), the
> `attestation` evidence kind (GRPH-542), `fleet.sign_off` as the first adapter
> (GRPH-544), and the refusal itself (GRPH-543) have all landed: `update_item` now
> refuses `done` without a valid attestation. Everything below about **Swamp** — the
> model, the mutation probe, the conformance and adversarial predicates, the tripwire
> workflow — remains design. Swamp is one adapter the port can accept, not a dependency,
> and none of it is written. This doc exists so the
> shape is agreed before code is written, and so the licensing boundary in
> [Licensing](#licensing-the-hard-boundary) is understood *before* an agent starts porting
> things it should not port. The tracked spec is **GRPH-P26 "Gated completion"**
> (approved); this document is the design detail behind it, and items that implement it
> link back here.

[Swamp](https://swamp-club.com/) is an open-source automation framework: agents author
typed **models** of external systems, wire them into declarative **workflows**, and every
method run produces immutable, versioned **data**. Secrets live in **vaults** and are
injected at runtime, so an agent operates real systems without the keys entering its
context. It is a CLI — anything that can run a shell can drive it.

This document argues that running Graphban *beside* Swamp produces measurably better code
than running either alone, describes how the two connect, and is honest about what it
costs.

**They are separate products, and the integration is optional.** Graphban's core never
learns the word "Swamp." No Swamp code is copied into this repo, and Graphban continues to
run fully offline with `docker compose up` and no external services.

## The gap this closes

Graphban knows what *done* means. It does not require anyone to prove it.

`update_item` validates a status transition for **membership in a list** and nothing else
(`backend/app/services/items.py:274`):

```python
if fields["status"] not in STATUSES:   # backlog, next, in_progress, review, done, blocked
    raise ValueError(f"invalid status: {fields['status']}")
```

The real gate exists, but it lives one module over in `fleet.sign_off`
(`backend/app/services/fleet.py:1171`), and reaching it requires a reviewer role:

```python
if needs_adversarial_evidence(item) and not items_svc.has_effective_sabotage(merged):
    raise MissingAdversarialEvidence(...)
```

So there are two paths to `done`, and only one of them is guarded. **An agent working
without a reviewer reaches `done` by writing the string `"done"`.**

That is this repo's own recurring finding — *the implementation is right, the reasoning is
in prose, and prose does not fail* — promoted from the test level to the system level. It
is the same class as GRPH-466 (a guard that counted its own arguments) and GRPH-540 (a
deferral whose trigger fires nothing), and it deserves the same fix this repo always
reaches for: not a convention, a check.

Swamp supplies the primitive Graphban lacks. A model declares `checks` that the runtime
evaluates **before** a method runs:

```ts
"verification-clear": {
  appliesTo: ["link_pr"], labels: ["policy"],
  execute: async (ctx) => result.allPassed
    ? { pass: true }
    : { pass: false, errors: [`Verification failed (${result.stepsFailed} step(s) failed).`] }
}
```

`pass: false` refuses the method. The agent's cooperation is not an input.

## Why the pair is stronger than either alone

This is the part worth reading. The two tools fail in *opposite* directions, and each one's
failure is the other's strength.

**Swamp alone gives you refusals with no memory.** A gate blocks, the agent reads a
one-line error, works around it, and the next agent hits the same wall for the same reason
next week. Nothing accumulates. Teams learn to route around gates whose reasons they never
see, and a gate that is routinely bypassed is worse than none — it reads as protection
while protecting nothing.

**Graphban alone gives you memory with no teeth.** Memory shards, `extract_lessons` on
`done`, semantic recall through `search_memory`, PRDs that state intent — a genuinely good
record of what was learned. And none of it stops anything. The lessons sit in prose, and
prose does not fail.

Run them together and the loop closes:

1. A gate **refuses** — critical adversarial finding unresolved, mutation probe broke
   nothing, deviation from plan unjustified.
2. The refusal is written to the item as **evidence**, with the failing predicate named.
   Evidence appends (`append_evidence`), so the record only grows.
3. On completion, `extract_lessons` turns the refusal and its resolution into a **memory
   shard**.
4. The next agent planning similar work hits that shard through `search_memory` or
   `related_work` — *before* writing code.
5. It clears the gate first time.

**Refusals become lessons; lessons prevent refusals.** Neither half compounds on its own.
Swamp's data is searchable by tag, not by meaning, so it cannot surface "we hit something
like this before." Graphban has semantic recall but nothing that forces an agent to consult
it. Together the enforcement generates the corpus and the corpus reduces the enforcement.

That is the case for running both, and it is specific to these two products.

### The second-order benefit

A gate that can refuse changes what agents write *before* the gate runs. The mechanism is
already documented in this repo — `fleet.py` states it precisely:

> Convert a hoped-for behaviour into something the server checks.

An agent that knows `complete` will re-run its mutation probe writes a test that can
actually fail. An agent that knows deviations require written justification either follows
the plan or says why not. The gate's value is mostly in the code that never has to be
refused.

## How it connects

### An attestation port, not a Swamp integration

Graphban grows a **`gate` scope** on API keys. Only a key carrying it may move an item to
`review`/`done` or attach `kind: "attestation"` evidence. Agent keys do not carry it.

This sits naturally under [design philosophy](design-philosophy.md) principle 2 —
*capability and authority are separate contracts* — and reuses the scope machinery that
already bounds the MCP manifest. Enforcement stops depending on agents behaving and starts
depending on what their credential can express.

Graphban does not care who holds the gate key:

| Adapter | What it attests | Cost |
| --- | --- | --- |
| **Swamp** | Full check suite over immutable run data | A second runtime |
| GitHub Action | CI is green | Nearly free |
| `fleet.sign_off` | Reviewer verdict + adversarial evidence | Exists today |
| A human | Personal judgement | A person |

Swamp is the richest adapter, not a dependency. The feature is worth building with Swamp
absent.

### Direction of travel

**Swamp calls Graphban over HTTP.** One Swamp model instance per item, with
`globalArguments: { itemKey, graphbanUrl, graphbanApiKey }`, the key drawn from a vault.
Ordinary API client; nothing crosses the license boundary but JSON.

Graphban does **not** call Swamp on any critical path. An attestation carries the Swamp run
ID; Graphban stores it opaquely. Confirming that the ID resolves is an optional
reconciliation job, never a blocking read. Core stays independent and offline-capable.

### Phase mapping

Don't reconcile the state machines. Graphban's six statuses are a human kanban; Swamp's
thirteen phases are the fine-grained truth, shown read-only on the item.

| Swamp phase | Graphban status |
| --- | --- |
| `created`, `triaging`, `classified`, `plan_generated` | `next` |
| `approved`, `implementing` | `in_progress` |
| `verifying`, `pr_open`, `pr_failed`, `releasing`, `summarizing` | `review` |
| `done` | `done` |
| any phase with a blocker set | `blocked` |

## The gates worth having

Ranked by predictability gained per unit of work. The selection criterion is that each one
**can fail**.

1. **Terminal transition.** `complete` gated on stored evidence via the existing
   `has_effective_sabotage`. The predicate is already written and tested — it needs a caller
   that can refuse and a credential boundary.
2. **Verification as an artifact.** Swamp's verification result records
   `workflowRunId`, `commit`, `branch`, `allPassed`, and per-step outcomes. A run ID that
   must resolve cannot be typed into existence. This is the structural answer to the
   fabricated-completion class.
3. **Mutation probe.** `has_effective_sabotage` requires `tests_failed > 0` — but *the
   agent supplies `tests_failed`*. A Swamp model that applies the mutation, runs the suite,
   and records the count as immutable data closes the last self-report in the one gate
   Graphban already enforces.
4. **Plan conformance.** Every step scored `implemented | deviated | partially_implemented |
   missing | added`, with justification **required** when not `implemented`. Maps onto
   `prd_section` and `section_fingerprint` drift detection. Catches silent scope creep.
5. **Adversarial findings.** No unresolved critical/high finding may pass `approve`.
   `fleet.py` already argues why this role must diverge where a reviewer converges; the gate
   is what makes the argument load-bearing.
6. **Deferral tripwires (GRPH-540).** Swamp workflows take schedule triggers. A nightly run
   evaluates each blocked item's trigger predicate and returns crossed ones to `backlog`
   with evidence. "Someone remembers to look" becomes a state transition.
7. **Release gate.** `scripts/smoke-deployment.sh` becomes a workflow step; `ship` refuses
   on failure.

### Worth stealing regardless

Swamp enforces a three-minute cooldown between linking a PR and reporting its outcome, so
CI has time to actually run. It kills an entire failure class — *reporting green before
green existed* — and is a few lines wherever we record PR state.

## Running both

Graphban is unchanged: `docker compose up`. Swamp installs separately and keeps its data in
`.swamp/` in the repo (or a shared filesystem, or an S3 datastore with distributed locking
for teams; `swamp serve` is the team deployment).

```bash
curl -fsSL https://swamp-club.com/install.sh | sh     # review before piping to a shell
swamp init --tool claude                              # also: cursor, opencode, codex
swamp vault set graphban-api-key                      # a gate-scoped key from Settings → API Keys
swamp model method run @graphban/item-lifecycle complete --itemKey GRPH-123
```

The last command is the whole point: it either writes `done` to Graphban, or it refuses and
says which predicate failed.

Swamp teaches agents through skills (`.claude/skills/`, `.cursor/skills/`) rather than MCP —
so Graphban's MCP tools and Swamp's CLI sit side by side in the same agent with no conflict.
Graphban answers *what should I build and what was learned*; Swamp answers *may this
advance*.

## Licensing: the hard boundary

**Graphban** is `FSL-1.1-Apache-2.0`. **Swamp** is AGPL-3.0 plus the Swamp Extension and
Definition Exception. The exception permits writing extensions and definitions and licensing
them "under terms of your choice, free-open or proprietary" — but only for work that
interoperates through Swamp's documented extension API **and**:

> 4. do not include copies of any Swamp source code, modified or unmodified.

AGPL cannot be relicensed under FSL. So:

- ✅ Write an original Swamp model against the documented extension API.
- ✅ Keep it in its **own repository** — separate license, separate toolchain (Deno/TS).
- ❌ Do not copy any file from the Swamp repo into this one, in whole or in part.
- ❌ Do not "port" or "adapt" a Swamp source file. Read the pattern, close the file, write
  from the spec. An agent asked to port a file reliably produces something substantially
  similar to it, which is exactly what condition 4 forbids.
- ❌ Do not build a generic MCP passthrough that re-exposes Swamp's extension API. The
  exception withdraws itself from anything that becomes "a practical substitute for Swamp."

Architecture is not what condition 4 protects; literal code is. A `checks` block returning
`{pass, errors}` is the documented API. Someone else's file with the names changed is not.

## What it costs

Stated plainly, because a doc that only sells is the same failure this integration exists to
fix.

- **A second runtime.** Swamp is Deno/TypeScript; this repo is Python + React + Docker. That
  is a real toolchain to install, version, and keep working in CI.
- **Latency on every transition.** Gates run before methods. A slow verification workflow is
  felt on every completion.
- **Gates can become theatre.** A check whose predicate is weak passes everything and reads
  as protection. Every gate added here needs a **refusal test** — one that puts the system in
  the state the gate exists to catch and asserts the refusal — or it repeats GRPH-466 one
  level up.
- **A second place where policy lives.** Two systems means two places to look when something
  is blocked. The phase field on the item is what keeps that navigable; without it this gets
  confusing fast.
- **Third-party coupling.** Swamp is young (first commit January 2026). Keeping it behind the
  attestation port, as one adapter among several, is what makes that acceptable.

## See also

- [Design philosophy](design-philosophy.md) — principles 2 and 4, which this extends
- [Tracker](tracker.md) — item statuses and the evidence field
- [MCP tools](mcp.md) — the tool surface agents drive Graphban through
- [Swamp](https://swamp-club.com/) · [source](https://github.com/systeminit/swamp)
