# Vendor adapters — support matrix

PRD-22 S2. Each adapter declares exactly four things: argv construction, config format
and where it is written, prompt delivery, and version range. Four headless CLIs that
agree on none of it.

**Selection is explicit, never inferred.** `gbfleet up --adapter claude` names the
vendor. There is no scan of PATH for whichever agent CLI happens to be installed,
because that produces a fleet whose composition nobody chose — quietly defeating the one
thing the supervisor is uniquely able to enforce. Resolving the *named* vendor's binary
on PATH is a different act; `--binary` overrides it.

## Getting from the Fleet view to a running supervisor (GRPH-556)

The Fleet view's Wave tab mints **seats** — one per agent, each granting a role for one
session. Those are the input `gbfleet` consumes, and the panel now hands you the file and the
command directly, under *Run these seats under a supervisor (advanced)*.

**Two credentials, and mixing them up is the usual first failure.**

| | what it is | where it comes from |
|---|---|---|
| `GBFLEET_API_KEY` | the supervisor's own credential | Settings → API Keys |
| a seat | one child's role for one session | the Wave tab |

The supervisor authenticates with an ordinary API key; the seats are what it gives its
children. Its own reach is deliberately narrow — `fleet_status` and `propose_allocation`,
nothing that claims work — so a supervisor cannot quietly become a worker.

Neither travels on argv. The key comes from the environment and the seats from a file,
because argv is world-readable.

**Seat mode is still the normal way to run a fleet**: paste a prompt into a terminal and the
agent registers itself. The supervisor is the advanced path — it cuts a worktree per child and
reaps them when the wave ends, which is worth it once you are running several at once and not
before.

**A wave** is one round of work. Its name becomes the branch prefix, and ending it revokes
every seat and releases every lease that wave issued. Two waves exist so you can end one
without stopping the other.

## The matrix

| vendor | version seen | range | MCP config | prompt reaches the child by | seat inside the worktree? |
|---|---|---|---|---|---|
| `claude` | `2.1.233 (Claude Code)` | 2.0 – 3.0 | `--mcp-config <path>` | stdin | **no** — private temp file |
| `gbagent` | `gbagent 0.1.0` | **exactly `0.1.0`** — a pin, not a range | `--mcp-config <path>` | `--instruction-file <path>` | **no** — private temp file |
| `cursor-agent` | `2026.04.17-787b533` | 2026.1 – 2027.1 | none; reads `.cursor/mcp.json` from the project dir | stdin | **yes** — forced |
| `grok` | `grok 1.0.5 (5115b46bc909) [stable]` | 1.0 – 2.0 | user-level `~/.grok/config.toml`; **unresolved** | `--prompt-file <path>` | yes — `.grok/mcp.json`, see below |
| `codex` | — | — | — | — | **not implemented** |

Every row above was read off a binary that was actually run on macOS, except `codex`.
`gbagent` is the one first-party row — see below for why its version column looks different
from the rest.

## Naming a model (GRPH-483)

Every vendor takes one and spells it differently. The supervisor **carries** the value and
chooses nothing — PRD-22 §1 is explicit that it "does not choose models for subagents", and
it could not if it wanted to: a seat's role is fixed server-side at mint and opaque until
redeemed (D-j). The caller names the model exactly as it names the vendor.

Omit it and the argv is byte-identical to before this existed.

| vendor | flag | can the model be checked before spawning? |
| --- | --- | --- |
| `claude` | `--model <model>` — alias (`opus`, `sonnet`, `fable`) or full name | **no.** No listing flag exists, so a named model is passed through UNCHECKED and the vendor is what refuses it |
| `cursor-agent` | `--model <model>` | **sometimes.** `--list-models` works, but an account with no entitlements answers *"No models available for this account"* — a fact about the account, not the model, so that case passes through too |
| `grok` | `-m <MODEL>` (also `--model`) | **yes.** `grok models` enumerated `grok-4.6` (default) and `grok-4.5` on the machine this was written on |
| `gbagent` | `--model <model>` | **when an endpoint is configured.** `gbagent models` asks it what it serves; with no `GBAGENT_BASE_URL` there is nothing to ask and the model passes through |

**Checked and unchecked must not read the same.** A validated model and one nobody could
verify are different states, and collapsing them would let `claude` look as guarded as
`grok`. Where the vendor can be asked, an unknown model refuses at `resolve()` — beside the
version check, and for the same reason: a model the vendor rejects produces a child that
starts, fails and never registers, which is indistinguishable from a broken adapter until
`await_registration` gives up, and blames the wrong component meanwhile.

**An empty listing is not a listing of zero valid models.** `known_models()` returns `None`
for "cannot be asked" and a non-empty set for "asked and answered". Treating an empty result
as an exhaustive list would refuse every spawn on an account that simply has no entitlements
yet — which is a working setup, broken by a check that was supposed to help.

## Per-vendor tuning (GRPH-484)

Beyond the model, two vendors expose one knob each. These are **not** uniform and are not
pretended to be — an adapter declares what it has, and `resolve()` refuses anything else
**by name rather than ignoring it**. A silently dropped knob lets a caller believe it asked
for cheap reasoning and pay for expensive: the setting evaporates, the bill does not.

| vendor | knob | validated? |
| --- | --- | --- |
| `claude` | `--fallback-model <a,b>` | no — no listing flag exists to check a name against |
| `grok` | `--reasoning-effort <effort>` (alias `--effort`) | no — see below |
| `cursor-agent` | none | n/a; either knob is refused |
| `gbagent` | `--turns <n>` and `--window <tokens>` | n/a — neither is a name to validate, and neither has a default anywhere |

**Both exist because a spawned child is unattended.** An overloaded model on an interactive
session is a wait; on a child it is a dead registration window — the process starts, cannot
get a model, never registers, and `await_registration` kills it, after which the supervisor
reports the *adapter* as broken. `--fallback-model` turns that into a slower child.

**`--reasoning-effort` accepts anything the CLI is concerned with.** Measured 2026-08-24
rather than assumed: `--help` says only *"Reasoning effort for reasoning models"* and
enumerates nothing, and `grok --reasoning-effort bogus-value models` is accepted without
complaint. So the accepted set is not discoverable from the binary, and declaring one here
would be exactly the fabrication `codex.py` refuses to make. It passes through unchecked and
this table says so.

## `gbagent` — the first-party adapter (PRD-24 D8)

It sits in the registry on the same terms as the rest and gets no special handling in
`resolve()`. G4: deleting it must change nothing about how the fleet is arbitrated. Being
ours buys better flags, not authority.

**Its version is an exact pin rather than a range, and that is not tidiness.** The range
machinery exists because three vendors release on their own schedules and we find out
afterwards. `gbagent` ships in this same wheel as the supervisor, so the only mismatch that
can occur is a `gbagent` on `PATH` from a **different install** — and a range would accept
exactly that. The pin is read from `gbfleet.__version__` rather than written out, because a
literal would refuse the next release the moment somebody bumped one file and not the other.

*Releasing bumps this table.* `verified_against` is the version the suite actually resolved,
and for this row it is re-verified on every CI run rather than observed once on a laptop —
`test_adapters.py` runs the real binary. When the package version changes, the `0.1.0` above
changes with it, and a test says so rather than letting the matrix go quietly stale.

**Two knobs, neither of them a name to check.** `--turns` is the budget from D6 (one turn is
22–45s against a local model) and `--window` is the model's context size, of which compaction
takes 70% (D7). Neither has a default in `loop.run` and neither gets one here: assume the
window too large and a run dies of an overflow compaction could have prevented; assume it too
small and it compacts constantly and throws away the 262k that made a local model worth using.

**Exit codes mean something here, because they are ours to define.** `exit_meaning()` reads
them out of `gbagent.loop` so there is one definition rather than two that drift:

| code | meaning |
| --- | --- |
| `0` | finished — the normal end of a worker's life (D-c) |
| `75` | stuck: turn budget spent, evidence written, item released, worktree salvaged |
| `70` | the handoff note could not be written, so the item was **not** released |
| `78` | refused before starting — no `.gbagent.toml`, no endpoint, unreadable seat |
| `69` | the model endpoint could not be reached |
| other | crash |

The supervisor tells surrender from failure by reading that, rather than by parsing stderr.
Exiting `0` on a give-up was rejected in D6: `ps` and the supervisor's record would show a
clean finish for a run that achieved nothing.

### Which model can actually build something

`--model` takes any string and `gbagent models` lists what the endpoint serves. Neither says
which of them can *build an item*, and the PRD-24 S7 walk measured a difference large enough to
decide whether this adapter is worth running at all.

| model | runs | outcome |
| --- | --- | --- |
| `qwen3.6:35b-a3b-coding-mtp-det` | 2 | built GRPH-497 and GRPH-501, **both signed off**, ~12 min each |
| `qwen3-coder:30b` | 3 | 93 minutes, **zero usable lines** |

Same harness, same items, same budget, same afternoon. The three `qwen3-coder` runs failed three
different ways: a fabricated completion (moved an item to `review` with a `test` receipt having
changed nothing), an 850-line file replaced by a six-line placeholder, and a 55-minute run ended
by a single turn exceeding the request timeout.

**This is thin evidence and should be read as thin.** Two models, five runs, one repository.
`gpt-oss:20b` and `qwen3:30b-a3b` have never been tried against an item. `qwen3.6` was never
re-run, so the determinism its `-det` build claims is untested. What the table supports is "one
of these worked twice and one failed three times" — not a ranking, and not a prediction about a
model that is not on it.

**A coding-specialised build mattered more than parameter count.** 35B beat 30B, but they also
differ in training; nothing here separates those two variables, and the walk did not try.

**Nothing owns the routing question.** PRD-24 §4 declares model routing a non-goal and defers
"which model suits which role" to PRD-11 — and **as measured on 2026-08-26, PRD-11 was still
`draft` with no approved baseline**. So the variable the arc's value rests on was assigned to a
document that had not been approved. Worth knowing before anybody plans around it, and worth
re-checking rather than trusting: this is the one claim on the page that a ledger write can
falsify without touching the repository, so it is dated rather than stated flat.

**What is not wired yet.** `gbagent run` can orient itself — the seven graph reads of PRD-24 S6
are advertised from the server's own manifest — but it cannot CLAIM its own work: `claim_next`
is deliberately absent from `coord.WORKER_TOOLS`. It refuses at startup with exit 78 naming the
slice that owns the gap, rather than starting, doing nothing useful and exiting 0.

## Versions do not share a scheme

Measured, not assumed. `claude` is semver, `cursor-agent` is **CalVer with a git hash**,
`grok` is semver behind a name. There is no single comparison that fits all three.

What does fit is the first run of dotted numbers, compared as a tuple of ints:
`2.1.233` → `(2, 1, 233)`, `2026.04.17-787b533` → `(2026, 4, 17)`. Both order correctly
*within their own scheme*, which is all a per-adapter range needs. A range is never
compared across vendors.

## Nothing carrying a credential goes on argv

argv is readable by every process on the machine. Declining to sandbox (PRD-22 D-k) is a
different thing from publishing a live seat to `ps`.

- The **API key** lives in the MCP config file, written 0600.
- The **enrolment code** lives in the instruction file, written 0600, and reaches the
  child on **stdin** (`claude`, `cursor-agent`) or by **path** (`grok --prompt-file`,
  `gbagent --instruction-file`).
- argv carries only paths and a generic pointer sentence that says "follow the
  instructions on standard input".

## The work phase is derived, never reported (GRPH-522)

The roster answers "who is out there" and "what is stuck with them". It now also answers
"what are they DOING with it" — but the phase does **not** come from the adapter, and no
adapter is asked to send it.

That is the whole design constraint. We own `gbagent`'s loop and can make it report
anything; we own none of `claude`, `cursor-agent` or `grok`, and `codex` is not
implemented. A reported phase would be populated for one adapter out of four, and a blank
column reads as an idle agent — the failure would look like the fleet working.

So the server derives it in `holding_phase()` from signals every vendor already writes
through the ordinary MCP surface: the item's status, its `blocker`, its `pr`, its evidence
receipts, and `bounce_reason`. Adding an adapter requires nothing; the phase works for a
vendor child on the day it first registers.

| phase | derived from |
|---|---|
| `stale` | the AGENT is offline or quarantined |
| `blocked` | `blocker` set, or status `blocked` |
| `review` | status `review` |
| `integrating` | a `pr` is recorded — pushed, so CI and a reviewer are what is outstanding |
| `verifying` | a `test` or `sabotage` evidence receipt exists |
| `building` | status `in_progress` |
| `claimed` | held, but still `next`/`backlog` |
| `unknown` | nothing matched — an admission, and its basis says so |

`stale` is checked first and outranks everything. An agent that dies mid-item leaves an
item that says `in_progress` forever, so a phase derived from the item alone would render
a dead worker as busy, indefinitely. Phase is displayed on an AGENT row, which makes it a
claim about the agent — this is the repo's recurring defect class, where the absence reads
as clean.

Rework is reported as a separate `bounced` flag rather than a `fixing` rung, because
activity and rework are independent: folding them together forces an arbitrary precedence
against `verifying`, and the bounce would vanish from the row the moment the agent ran a
test. Every holding also carries `phase_basis`, the literal signal that produced the
phase, because an inference nobody can check is one they have to trust.

None of this widened the supervisor. Deriving it supervisor-side would have needed
`get_item_details`, and `ALLOWED_TOOLS` is pinned to `{fleet_status, propose_allocation}`
by exact equality — a display feature must not hand the supervisor a worker's authority.
The roster row carries more truth instead.

**What it still cannot tell you.** These are lifecycle positions, not activity. `building`
covers reading the codebase, editing, and thinking; a child compiling for ten minutes and
one stuck in a loop look identical. The only finer signal that exists is `gbagent`'s
per-turn trace (GRPH-506), which is unstructured, local to the spawn log, and first-party
only — which is exactly why it was not made the source.

## Two things that are not solved

**`grok`'s per-child seat.** Its MCP configuration is user-level
(`~/.grok/config.toml`) and `--help` shows no per-invocation config flag. A per-child
seat cannot be delivered through a shared user-level file without two children racing
for it. The adapter writes `.grok/mcp.json` into the worktree so the file is at least
per-child and salvage knows about it — namespaced to grok rather than borrowed from
Cursor, so it does not read as leftover from a vendor that is not running. Whether grok
reads it there is untested. A child that
cannot see its seat fails to register, and the bounded registration window is what
surfaces that — loudly, and blamed on the right adapter.

**`codex` is declared and deliberately not implemented.** It was not installed on the
machine this was written on, so its version string, its flags and its config mechanism
would all have been invented. A fabricated adapter fails in exactly the way S2 exists to
prevent: a child that starts, does not understand its arguments, and never registers —
burning a registration window and blaming the vendor for the supervisor's mistake.
`resolve("codex")` raises and lists the vendors that do work. Finishing it is four
declarations and a version string, for somebody with the binary in front of them.

## Headless means weakened permission prompts

`claude` gets `--dangerously-skip-permissions`; `cursor-agent` gets `--force`,
`--approve-mcps` and `--trust`. There is nobody to answer a prompt in a headless run.

PRD-22's risk table names this and answers it with the worktree boundary plus the
per-child config — and is explicit that this is **not a sandbox** (D-k, §7). The child's
cwd is its own worktree, so a bad worker cannot reach another's files. That is the whole
claim.
