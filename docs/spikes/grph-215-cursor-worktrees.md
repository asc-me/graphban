# GRPH-215 spike — collision clusters → Cursor parallel worktrees

**Goal:** decide whether Graphban's collision-aware clustering (AL-192) can drive Cursor
as a second execution backend behind the same adapter seam AL-201 opened for Grok Build,
and whether Cursor run output can auto-capture touchpoints. **Verdict: sound, phase it —
and do not build a second worktree owner.**

Ground truth: `collision_clusters` + `GET /items/collision-clusters` already ship
(AL-192). The fleet supervisor already runs `cursor-agent` in Graphban-owned worktrees
(PRD-22). Cursor CLI / SDK measured against public docs on 2026-09-02.

## The two halves, and what actually exists

**Graphban — `next_cluster` / `collision_clusters`.** Distinct clusters share no
touch-areas, so they are safe to run in parallel. Unchanged since AL-201.

**Cursor — no `--parallel`.** Grok Build's fit was exact because `grok --parallel`
spawns up to 8 sub-agents, each in its own worktree, and streaming-json reports
`files_modified`. Cursor CLI (2026) has:

| Cursor has | What it is | Use it? |
|---|---|---|
| `cursor-agent --print --output-format stream-json` | NDJSON tool events (`writeToolCall` / `readToolCall`) | **Yes — phase 1.** Isolated parser. |
| `cursor-agent --worktree` | One extra git worktree under `~/.cursor/worktrees/<repo>/<name>` | **No.** A second worktree owner would fight the fleet's trees. |
| `@cursor/sdk` `Agent.create({ local: { cwd } })` | TypeScript public beta, weekly churn (the ticket's own gate) | **No.** Do not take a TS dependency in the Python fleet. |
| `/orchestrate` as a stable CLI | Not on the 2026-09 CLI parameter list | **Not evidenced.** Do not invent it. |

Fan-out is Graphban's job: one non-colliding cluster → one process. Cursor does not
need to own the pool.

## Touchpoint auto-capture

Two sources, and they are not equal:

1. **Git-diff of the worker's branch** (PRD-22 S5, already shipping). Sees shell
   writes, not only editor tool calls. Ground truth when a Graphban worktree exists.
2. **`writeToolCall` / `editToolCall` / `deleteToolCall` on stream-json.** A subset.
   Pays off when there is no Graphban worktree (a captured log, an SDK cwd), and as
   extra union on reap so a write-tool event is not dropped if git has not caught it
   yet.

Reads (`readToolCall`) are not touchpoints. An empty parse is "the stream named no
writes", not "safe to send `[]` to `update_item`".

Prototype: `fleet/src/gbfleet/adapters/cursor_stream.py`. The NDJSON shape does not
leave that file. `CursorAgent.stream_touched` is the adapter method; other vendors
return `[]`. Reap unions via `touchpoints.including_stream`. Parse-only write-back
without a wave is `record.from_cursor_stream`.

`cursor-agent` launch now passes `--output-format stream-json` (before the positional
prompt pointer — a flag after it is prompt text). This is **not** a debug log; the
debug gap for Cursor stays announced.

## Phased path

1. **Parse-only (this spike).** Parser + launch flag + reap union + `from_cursor_stream`.
   No new orchestrator.
2. **Single-cluster runner.** Already the fleet: one seat, one Graphban worktree, one
   `cursor-agent`. Do not add `--worktree`.
3. **Wave orchestration.** Already `gbfleet up`. Cursor SDK parallel is out of scope
   until it exists as a measured, version-pinned CLI.

## Risks / constraints

- **Provider-neutral.** Cursor JSON must not leak past `cursor_stream.py`. Grok
  streaming-json, if revived, gets its own parser behind the same `stream_touched`.
- **Contract churn.** Cursor docs say field additions are backward-compatible;
  consumers ignore unknown fields. Malformed lines are skipped, not fatal.
- **`--worktree` collision.** Cursor-owned trees under `~/.cursor/worktrees` are a
  different namespace from `gb/` branches. Mixing them splits salvage, seat files, and
  measurement. Refuse.
- **SDK beta.** The ticket gated on TS SDK stability. It is still a public beta.
  The CLI stream is the stable-enough slice.
- **Collision ≠ conflict.** Same as AL-201: semantic deps are not always in
  touch-areas. CI + review still gate every merge.

## Deliberately out of the spike

Building a Cursor-native parallel runner; taking `@cursor/sdk`; using
`cursor-agent --worktree`. The Graphban side already exists. Phase 1 is the
parser and the CALL that writes what it parsed.
