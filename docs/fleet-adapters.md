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
| `grok` | `grok 1.0.5 (5115b46bc909) [stable]` | 1.0 – 2.0 | project-scoped `<worktree>/.grok/config.toml` (**TOML**), needs `--trust` | `--prompt-file <path>` | yes — `.grok/config.toml`, see below |
| `qwen-code` | `0.23.0` | 0.23 – 1.0 | `--mcp-config <path>` + `--allowed-mcp-server-names graphban`; the entry must be `httpUrl` | stdin | **no** — private temp file |
| `codex` | — | — | — | — | **not implemented** |

Every row above was read off a binary that was actually run on macOS, except `codex`.
`gbagent` is the one first-party row — see below for why its version column looks different
from the rest.

This table is *support*: what each binary needs to run. Which harness and model a `spawn`
resolves to for a tier is a different table — the **preference matrix** in
`fleet/src/gbfleet/matrix.toml` (PRD-37), one row per harness × model × lane × role × tier
with a status and the item that proved it. `codex` is a `status = "unregistered"` row there
so the file's existence never reads as support; `gbfleet doctor` prints every row against
this machine and what each role/tier would resolve to. See `fleet/README.md`.

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
| `qwen-code` | `-m <model>` | **no.** No listing flag, and worse than unchecked: `-m bogus-model-name` ran the configured default (`qwen3.7-plus`) with no warning anywhere (measured 2026-09-05 on 0.23.0). A named model for this vendor is a claim the binary will not enforce |

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

## Which model, and what that was measured on (GRPH-557)

The section above says how to NAME a model. This one says what happened when two were
actually run, because the answer turned out to matter more than anything else in PRD-24.

**Measured 2026-08-20 on `ms-s1-ubt`, against real items in this repository, five runs:**

| runs | model | outcome |
| --- | --- | --- |
| 1–3 | `qwen3-coder:30b` | 93 minutes, **zero usable lines**, three distinct failures |
| 4 | `qwen3.6:35b-a3b-coding-mtp-det` | a correct 9-line fix with 90 lines of tests, signed off to `done` |
| 5 | `qwen3.6:35b-a3b-coding-mtp-det` | closed a real **authority hole** at effort 3 — and was refused `sign_off` for having no sabotage receipt, which is the gate working |

The walk's own conclusion, kept in its words: *"`qwen3-coder:30b` cannot build an item in this
repository, and a newer coding-specialised model of similar size can. Model choice is the
variable."*

**Read this as a measurement, not a recommendation.** It is one repository, two models, one
box, one day. `qwen3-coder:30b` is not a bad model in general; it could not do this work here.
A newer coding-specialised model of similar size could. What transfers is the shape — that the
gap between two same-sized local models was the difference between *nothing* and *signed off*
— not the specific names, which will age.

**It says nothing about the vendor adapters.** `claude`, `cursor-agent` and `grok` were not
measured here. PRD-24's own §4 non-goal stands — *"not a model router; which model suits which
role is PRD-11's question"* — and the walk's finding is that this non-goal is where the arc's
value rests. That is a reason to record what is known, not to invent what is not.

The three runs that failed were not wasted: the completion guard and the truncation guard both
came out of them, and those are the sort of thing you only find with a model bad enough to
need them. What was wrong was the conclusion first drawn from them — that the cheap tier was a
false economy — which came from one model and did not survive a second.

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
| `qwen-code` | `--turns <n>` (spelled `--max-session-turns` on the binary) | n/a — a count, not a name |

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

## `qwen-code` — measured on 0.23.0, 2026-09-05 (PRD-37 D13)

Every line here was read off `qwen` 0.23.0 by running it against the live server, because the
obvious shapes were wrong in ways the binary does not report.

- **Headless prompt is stdin.** `--help` says the positional prompt "is appended to input on
  stdin"; stdin alone runs one-shot and `-o json` ends with a `result` record. Nothing that
  carries the seat touches argv.
- **`--mcp-config <path>` works, but only spelled `httpUrl`.** The seat's vendor-neutral
  `{"type": "http", "url": …}` entry is accepted without a word and never connects — the init
  record says `disconnected`, zero tools. The same URL and headers as `{"httpUrl": …}` connected
  and listed 57 `mcp__graphban__*` tools. The adapter rewrites the seat into that shape.
- **`--allowed-mcp-server-names graphban` is required.** Without it the child also loads every
  server in the operator's `~/.qwen/settings.json` — another credential, another identity, in
  the child's tool list. `--bare` is not the fix: it also drops the model-provider config and
  the child dies at once (`No auth type is selected`, exit 1).
- **A wrong `-m` is not refused.** `-m bogus-model-name` ran `qwen3.7-plus`. So the matrix row
  ships with **no model** and stays `unverified` until a spawn walk signs an item off.
- **Exit codes:** 0 normal; **55** `FatalBudgetExceededError` when `--max-wall-time` or
  `--max-tool-calls` is exceeded (the JSON tail names which); 1 when the run could not start.
- **Debug:** `-d` exists but writes to stderr with no file flag, so `--debug` gets nothing from
  this vendor and the support matrix says so.

Not yet done: a spawn walk. The row is a claim about a binary that connects, not about a model
that builds; that is what moves it to `verified`.

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

## `grok`'s per-child seat: solved, and how it was wrong

This section used to say the seat was unresolved and that grok's MCP config was
user-level only. Both halves were wrong, and the way they were wrong is worth keeping.

`grok mcp add --help` names two scopes: `user (~/.grok/config.toml)` and `project
(./.grok/config.toml)`. The project scope is **per-directory**, and each child's
worktree is its own directory — so the seats never race, and the fleet model works for
this vendor. The adapter now writes `<worktree>/.grok/config.toml`.

Three things had to be measured rather than assumed (grok 1.0.5, Windows 11 and macOS):

**It is TOML, and the table is `mcp_servers`.** Not `mcpServers`. The previous adapter
wrote `.grok/mcp.json`, a filename that appears nowhere in grok's Config Sources, so the
seat was read by nobody. Letting `grok mcp add` write the file and reading it back is
the only reason this is a fact rather than a guess:

```toml
[mcp_servers.graphban]
url = "https://cloud.graphban.dev/api/mcp"
enabled = true

[mcp_servers.graphban.headers]
X-API-Key = "..."
```

There is no `type` or `transport` key — grok infers HTTP from the presence of `url`.
`seat._as_toml` therefore *refuses* a non-HTTP server rather than rendering one that
silently becomes HTTP, and refuses unknown fields rather than dropping them.

**A repo-local server is gated on folder trust.** In a fresh worktree:

```
✗ folder untrusted (repo-local (project-scoped) server not started for an untrusted folder)
```

The child gets no tools and no error. `grok inspect` makes it worse by listing the
server as loaded while `grok mcp doctor` reports it never started — so the obvious way
to check agrees with the broken state. The adapter passes `--trust`, which is absent
from `--help` but accepted, and which grok's own bundled docs name as the mechanism
(`~/.grok/docs/user-guide/10-hooks.md`: `/hooks-trust` "or launching with `--trust`",
recorded in `~/.grok/trusted_folders.toml`, "the same gate that governs repo-local
MCP/LSP servers"). Pre-seeding that store works too, but it is a shared file and
concurrent children would race on it — which is the mistake this section used to
describe, just moved. `--trust` is per-invocation and races nothing.

**Project scope beats user scope on a name collision.** This one is security-relevant.
The operator running the supervisor is exactly the person likely to have `graphban`
configured in their own `~/.grok/config.toml`. If user scope won, every child would
connect with the *operator's* credential and take the operator's role, and seat-based
roles would mean nothing while appearing to work. Measured: the project file wins.
`test_a_childs_seat_beats_an_operators_user_level_server_of_the_same_name` holds it.

One caveat that changes how failure looks: **a grok child whose MCP server fails to
connect still runs to completion and exits 0.** Measured with a deliberately invalid
key — it answered its prompt and left. A broken seat is not a crash, it is an expensive
silence, and `registration_latency` is what turns it back into a signal.

A last trap for whoever reads `grok mcp doctor` next: **its *Config sources* block is
not trustworthy and its per-server verdicts are.** Two separate ways it misleads, both
measured:

- it reported `0 servers` for a project `.grok/config.toml` whose server it had plainly
  loaded and started;
- it never lists `.cursor/mcp.json` as a source at all, and credits servers loaded from
  it to `.mcp.json`.

The per-server `✓ server started` / `✗ folder untrusted` lines are the reliable signal.

**grok reads more project files than its own.** `.mcp.json` and `.cursor/mcp.json` in
the project directory are both loaded (measured — servers from each started). A worktree
is cut from the repository, so **anything the repo commits is in front of every grok
child**, added to whatever its seat provides. This repo commits no such file today; a
repo that did would be handing extra tools to every worker, and nothing in gbfleet is in
a position to stop it. What it *cannot* do is override the seat: on a name collision
`.grok/config.toml` wins, and two tests hold that down — one for the collision and one
control proving the committed file is genuinely loaded when nothing outranks it, because
"the seat won" and "the rival never entered" look identical from the outside.

## Before the first run: `gbfleet doctor`

Every trap in this document is one an operator meets on their first run, and several are
silent or blamed on the wrong component. `gbfleet doctor` asks them all before a child
exists:

```
gbfleet doctor --repo . --adapter grok --server https://cloud.graphban.dev
```

```
  [PASS   ] host platform — Windows (nt)
  [PASS   ] python version — 3.12.10
  [PASS   ] credential files can be kept private
  [PASS   ] repository — C:\Users\Alex\gbsrc
  [FAIL   ] repository does not commit a seat path — commits ['.grok/config.toml']
            the supervisor must write a child's seat there and would truncate your
            file; use user-scope config instead (grok mcp add --scope user)
  [PASS   ] supervisor lock is free
  [PASS   ] adapter grok — grok 1.0.5 [stable]
  [PASS   ] adapter grok supports --debug
  [UNKNOWN] server reachable — no --server given

  9 ok, 1 failed, 2 unknown
  unknown is not ok: these were not checked, not checked and found fine
```

**Three outcomes, not two.** `UNKNOWN` is a check that could not be made, and it is
neither counted as success nor left out of the summary. Two outcomes would force every
unanswerable question into one of the answers — which is the defect this repository
keeps finding, and it found another one here: the preflight's first run reported
`[UNKNOWN] supervisor lock is free — Permission denied` and turned up a live regression
that had made the supervisor unable to take its own lock (GRPH-600). A two-outcome
version would have printed `FAIL` or, worse, `PASS`.

Only `FAIL` sets a non-zero exit. Refusing to start because a check could not be *made*
would ground the fleet on a slow network, so unknowns are printed loudly and the
operator decides.

## Debug output: what each vendor can be asked for

`gbfleet up --debug` asks each child's CLI to write a debug log beside its stdout, and
emits a per-poll reading of what every child is producing. Measured from each `--help`,
not assumed, and the answers differ enough that the gaps matter more than the coverage:

| adapter | debug flag | what `--debug` gets you |
|---|---|---|
| `grok` | `--debug`, `--debug-file <FILE>` | vendor debug log + output sampling |
| `claude` | `--debug-file <path>` (its help says this implicitly enables debug mode) | vendor debug log + output sampling |
| `cursor-agent` | **none** | output sampling only |
| `gbagent` | **none** | output sampling only |

`cursor-agent` has `--output-format stream-json` and `--stream-partial-output`, which
give structured progress rather than a debug log. That is a different feature reached a
different way, and quietly substituting it **as debug** would be the fabrication
`codex.py` refuses to make. Launch does pass `--output-format stream-json` for
touchpoint capture (GRPH-215): write-tool paths are parsed in
`adapters/cursor_stream.py` and unioned onto the git-diff measurement. That is not a
debug log, and the debug gap above stays announced. `gbagent` is ours and has no flag either; it already has a per-turn trace
(GRPH-506) and simply no way to be told where to put it, so fixing that is real work
rather than a declaration.

**The gaps are announced, not left to be inferred.** A wave run with `--debug` prints
`NO DEBUG cursor-agent: no debug flag; output sampling only` and logs a
`debug_unavailable` event. An operator who asked for debug, saw a quiet log for a Cursor
child and concluded the child was fine would have been misled by the tool.

The flags are placed by each adapter rather than appended by the caller, because
`claude` and `cursor-agent` both end their argv with a positional prompt pointer and a
flag after it is read as prompt text.

## Two silences, and why both are printed

The supervisor reports a child as quiet from two independent kinds of evidence, and the
summary labels which:

- `QUIET <child>: wrote nothing for 900s (local)` — this machine watched the child's own
  log files stop growing. Needs no network and no cooperation from the vendor, which is
  what makes it the useful one under D-i.
- `QUIET <agent>: no heartbeat for 1200s (server)` — the roster stopped seeing it. A
  partition produces this without the first.

Neither stops anything. Silence is weak evidence: a child inside one long tool call is
legitimately silent — `gbagent`'s `run_tests` timeout alone is 1800s — and file writes
are buffered, so output arrives in bursts with real gaps between them. `--quiet-after`
sets when it is *reported* (default 300s), and `Limits.disowned_after` remains the only
thing that stops a child on silence, at 1800s and for the server-side signal only.

## One thing that is not solved

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

**`grok` gets nothing, and that is correct rather than an oversight.** This section used
to name two vendors and say nothing about the third, which reads as "grok needs
nothing" without ever claiming it. It does need nothing, and here is the measurement:
grok's own user guide says headless mode "accepts a single prompt, executes it with
**full tool access**, and returns the result" (user-guide 14). Tested rather than taken
on trust — a child launched with no approval flag at all, stdout to a file, stdin
`/dev/null`, no tty, was asked to create a file. It created it and exited 0.

That control matters because the obvious-looking change is wrong. grok has
`--always-approve` (alias `--yolo`, or `--permission-mode bypassPermissions`), and its
docs even recommend it for "Scripts, SDKs, CI, agent servers" — so adding it looks like
an improvement. It would be a no-op dressed as a fix, justified by a hypothesis that a
five-minute experiment disproves.

What *can* still stop a grok child is configuration rather than a missing flag: `deny`
rules, hooks, and `ask` rules matching a shell command's segments apply on top of any
mode, and an administrator can lock always-approve off. Those come from
`~/.grok/config.toml`, from **every project `.grok/config.toml` from the repo root
down**, and from `.claude/settings.json`. A child blocked that way does not crash — it
waits — which is now visible as a quiet child rather than as nothing at all (GRPH-579).

PRD-22's risk table names this and answers it with the worktree boundary plus the
per-child config — and is explicit that this is **not a sandbox** (D-k, §7). The child's
cwd is its own worktree, so a bad worker cannot reach another's files. That is the whole
claim. Worth noting for anyone revisiting D-k: grok does offer `--sandbox <PROFILE>` for
filesystem and network access. It is deliberately not used, because turning it on would
change the security posture the PRD reasoned about, and that is a decision to make on
purpose rather than a flag to add because it exists.

## A repository may not commit a seat path

The seat paths — `.grok/config.toml`, `.cursor/mcp.json` — are files a repository can
reasonably commit. `grok mcp add --scope project` writes exactly the first one. A
worktree is cut from the repo, so a committed one lands in every child's tree, and
`seat.write` truncates.

That is not only a lost MCP entry. grok merges `[permission]` rules from every project
`.grok/config.toml` from the repo root down, so this is where a repository states what
its agents may not do — and overwriting it removes the repo's own `deny` rules. Because
`SEAT_FILES` is excluded from salvage on purpose, the change is never committed, never
reported, and invisible for the rest of the child's life. The one signal that did fire
was wrong: at reap the path shows up as `credential_in_history`, i.e. "the worker
committed a credential", when the repository tracked it all along.

So `worktree.create` refuses, against the base commit, before the worktree exists:

```
<repo> commits ['.grok/config.toml'] at <sha>, and the supervisor has to write a
child's seat to that path. ... Remedy: stop committing it (user-scope config does the
same job — `grok mcp add --scope user`), or run the fleet against a checkout that does
not.
```

Refusing is loud and takes ten seconds to fix. Writing the seat anyway is silent and
changes what every worker in the fleet is allowed to do.

## Verified on the deployed instance

On 2026-09-05 a session delegated GRPH-731 to a bound seat and gbfleet resolved the
cheap tier through the preference matrix with no `--tier` flag. The resolution was
automatic: the seat carried the tier from the ledger, the supervisor looked it up in
`matrix.toml`, and the child started on the harness and model that row names — no
caller intervention, no override. The whole path from delegation to a running worker
is what this document describes, and it worked end to end on a live server with real
items.
