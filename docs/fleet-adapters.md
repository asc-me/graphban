# Vendor adapters — support matrix

PRD-22 S2. Each adapter declares exactly four things: argv construction, config format
and where it is written, prompt delivery, and version range. Four headless CLIs that
agree on none of it.

**Selection is explicit, never inferred.** `gbfleet up --adapter claude` names the
vendor. There is no scan of PATH for whichever agent CLI happens to be installed,
because that produces a fleet whose composition nobody chose — quietly defeating the one
thing the supervisor is uniquely able to enforce. Resolving the *named* vendor's binary
on PATH is a different act; `--binary` overrides it.

## The matrix

| vendor | version seen | range | MCP config | prompt reaches the child by | seat inside the worktree? |
|---|---|---|---|---|---|
| `claude` | `2.1.233 (Claude Code)` | 2.0 – 3.0 | `--mcp-config <path>` | stdin | **no** — private temp file |
| `cursor-agent` | `2026.04.17-787b533` | 2026.1 – 2027.1 | none; reads `.cursor/mcp.json` from the project dir | stdin | **yes** — forced |
| `grok` | `grok 1.0.5 (5115b46bc909) [stable]` | 1.0 – 2.0 | user-level `~/.grok/config.toml`; **unresolved** | `--prompt-file <path>` | yes — `.grok/mcp.json`, see below |
| `codex` | — | — | — | — | **not implemented** |

Every row above was read off a binary that was actually run on macOS, except `codex`.

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
  child on **stdin** (`claude`, `cursor-agent`) or by **path** (`grok --prompt-file`).
- argv carries only paths and a generic pointer sentence that says "follow the
  instructions on standard input".

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
