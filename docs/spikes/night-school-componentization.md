# Spike — Night School componentization into Graphban

**Goal:** decide whether Night School (a working, offline session-transcript lesson miner)
can be decomposed into components Graphban absorbs, giving the platform a real
learning loop — and whether that loop can decide *when to create a skill*.
**Verdict: sound, and smaller than it looks.** Night School is three separable layers.
Two have no counterpart in Graphban and port cleanly. The third is already built here,
better, and porting it would be a duplication bug.

Source under assessment: `cursor-commands/night-school/` (~5.9k lines Python, stdlib-only,
zero harness imports in `core/`). Paths below are relative to that package unless prefixed
`backend/`, which means this repo.

> Scope note: this spike assesses **componentization and fit**. It does not assess the
> hosted/multi-tenant rollout — see `docs/design/cloud-tenant-design-prompts.md` for that
> layer. The cross-project promotion risk in §5 is the point where the two meet.

---

## 1. The decomposition

Night School reads as one nightly pipeline, but it is three layers with clean seams:

| Layer | What it does | Key modules | Ports? | Counterpart here |
|---|---|---|---|---|
| **A. Ingest** | Transcript → normalized `Event` (9 kinds), watermarked and incremental | `core/events.py`, `adapters/*.py` | **Cleanly** | **None** |
| **B. Lifecycle** | Semantic dedup, corroboration, promote/decay | `core/dedup.py`, `core/consolidator.py` | **Do not port** | **Yes — richer** |
| **C. Artifact engine** | Tier classify → draft → install → measure → retire | `core/classifier.py`, `core/drafter.py`, `core/artifact_adapter.py`, `core/telemetry.py` | **Cleanly** | **None** |

The seam is real, not aspirational: `core/` has zero imports from any harness, and
everything harness-specific sits behind three documented interfaces
(`docs/ADAPTER_GUIDE.md` in the source repo). Adding a harness means writing a subset of
those three and registering one dict entry.

## 2. Overlap map — where Graphban already wins

| Concern | Night School | Graphban | Verdict |
|---|---|---|---|
| Semantic dedup | model-judgment pass, convergence-checked (`core/dedup.py`) | pgvector + thresholds `_SIM_DUP=0.95`, `_SIM_REJECTED=0.85`, `_SIM_STRONG=0.88` (`backend/app/services/memory.py:120-125`) | **Graphban** — vector-native, no model call |
| Corroboration | recurrence counter on `lesson` | `_score_shard` support counting + `_corroboration_pool` | **Graphban** |
| Human review gate | file-based queue + CLI | review queue + web UI + `memory_write_mode` (`review`/`auto`/`trusted`, migration `0040`) | **Graphban** |
| Auto-reject near-dups | — | `memory_auto_reject`, on by default (migration `0036`) | **Graphban** |
| LLM judge | — | `_llm_judge`, opt-in per project | **Graphban** |
| **Transcript ingest** | 3 adapters: append-only JSONL, content-addressed blobs, mutable sqlite | **none** — `extract_lessons` reads an item's title + description (`backend/app/services/insights.py:11`) | **Night School** |
| **Recurrence gate** | promote iff `recurrence >= 3` **and** `distinct_sessions >= 3` (`core/consolidator.py:72-77`) | `support >= 2` on a single scoring pass | **Night School** |
| **Decay / staleness** | 45d decay, 120d retire window | none | **Night School** |
| **Tier classification** | 8 tiers incl. `skill` (`core/classifier.py:22`) | **absent** | **Night School** |
| **Artifact rendering** | full SKILL.md / hook / rule / agent drafts (`core/drafter.py:36-82`) | **absent** | **Night School** |
| **Usage telemetry on generated artifacts** | `extract_usage_signal` + staleness sweep (`core/telemetry.py:172`) | **absent** | **Night School** |

**The critical read:** Graphban's `extract_lessons` takes a work item's *title and
description* and emits text shards. Night School takes *raw session transcripts* —
thousands of events including tool calls, exit codes, guard fires, and correction signals —
and emits *installed, usage-tracked files*. Same word, different magnitude. Graphban learns
from what you wrote down **about** the work; Night School learns from what **happened**
during it.

### Why layer B must not be ported

Design philosophy §1 — "one source of truth, and the repo enforces it… duplication is a bug
we make mechanically impossible." Graphban's triage path is the owner of *is this candidate
worth keeping*. Porting Night School's dedup/consolidator would create a second scorer with
different thresholds reaching different verdicts on the same shard. The correct move is to
**extend `_score_shard` with the two signals it lacks** (distinct-source recurrence, and a
decay clock), not to run a parallel lifecycle beside it.

## 3. When to create a skill — already solved

This was the question with the least expected work behind it. The full gate exists:

```
extractor      lesson candidate, classed (correction / drift / preference)
dedup          semantic merge of restatements
consolidator   PROMOTE iff recurrence >= 3 AND distinct_sessions >= 3
               (>= 2 when class == "correction" — corrections earn trust faster)
classifier     tier ∈ {fact, rule, hook, skill, agent, allowlist, update, delete}
               "skill: a reusable multi-step procedure worth a skill doc"
               + scope resolution: if an artifact already owns this scope, UPDATE it
drafter        renders a COMPLETE SKILL.md (frontmatter + body), not a stub
queue          human approves
telemetry      tool_call Skill{skill: name} → use_count_30d
               zero use in window → staleness sweep queues a RETIRE rec
```

Three properties here are hard-won and worth preserving verbatim:

**The n=1 gate.** Requiring *both* total recurrence and distinct-session count
(`core/consolidator.py:72-77`) blocks the failure where one long session repeats itself into
false confidence. Graphban's `support >= 2` counts corroborating shards, not distinct
origins — a subtly weaker guarantee.

**The delete path.** Most self-improving systems only create. This one measures whether the
artifact it generated is ever used and proposes retirement when it isn't. That is what keeps
a generated corpus from becoming landfill.

**Honesty about what is measurable.** `core/telemetry.py` records usage only where a clean
structured signal exists — skills (`tool_call` where `tool_name == "Skill"`) and agents
(`subagent_type`) are observable; hooks and Cursor `.mdc` rules are **not**, because the
transcript never names the firing script. Those are inventoried but marked `measurable=0`
and excluded from staleness rather than given a fabricated signal. Any port must keep that
distinction or the retire path will delete working hooks for lack of evidence.

## 4. "Plug into the apps" — two readings, pick one

| | (a) Mine the agent sessions that **build** the apps | (b) Make the apps **themselves** learn from end users |
|---|---|---|
| Input | Claude Code / Cursor transcripts across all repos | App-specific user behavior |
| Reuse | ~all of it — config change plus the ingest port | The **lifecycle model** only; ~no code |
| Tier taxonomy | Already correct (targets the harness) | Needs a new one per product |
| Language | Python → Python. Direct | Python → Next.js. Reimplementation |
| Effort | Low | Product-sized |

(a) is what the current code does and what this spike recommends. (b) is a different
product that happens to share a shape. They should not be conflated in planning — the
overlap is the state machine, not the package.

## 5. Risks

**Leakage is the blocker, and it is ordering-sensitive.** Transcripts contain absolute
paths, hostnames, tokens, and customer data. Graphban's README commits to local-Docker-first
with no external services. Ingest is precisely the step that pulls raw transcript content
into Postgres, so **scrubbing must land in the same change as ingest, not a phase later**.
Scrub at extraction, before a shard row is written — not at publish time, because a
candidate is already persisted and searchable.

**Wrong-context lessons.** "Always use pnpm here" is true in `solascriptura` and false
elsewhere. Platform-wide learning needs a promotion **ladder** — project-local → org →
universal — with a higher bar at each rung. Graphban's `project_id` plus its existing global
scope (`_includes_global`) is the natural home; Night School has no such dimension because it
was built single-user, single-machine.

*Resolved in grill (GRPH-P16):* the org rung gates on **3 independent observations**, where
independence comes from either a distinct project or a distinct user —
`independence = distinct_projects + max(0, distinct_users - 1)`. A **combination, not a
conjunction**: Graphban's local-first default is single-user, so a user-count *requirement*
would make org promotion permanently unreachable on a solo install. Under one user the
formula degrades exactly to `distinct_projects >= 3`. Night School contributes only the
session half of this — the user dimension has no counterpart there.

Known limitation carried into the PRD: two teammates following the same house convention are
*correlated*, not independent, and can co-sign a lesson that is really one shared habit. Same
weakness applies to projects sharing a template. Accepted at the org rung, where a human still
approves; flagged as open for the universal rung.

**DE-SCOPED 2026-08-11 (GRPH-306 → GRPH-356). The org rung is not built, and this records why
rather than leaving the design reading as though it shipped.** The other two rungs of the
ladder are real and verified in code — distinct-source recurrence (`memory.py:224`, applied as
a veto on an accept rather than a new accept path) and the decay clock (`age_state`,
`memory.py:33`). The scope rung is not, and the decision was to defer it rather than build it
now, because **both of its inputs are missing**:

- `distinct_users` — nothing records a user on a shard at all. An ingested shard's `origin` is
  `ingest:<harness>:<state>` and its `source` is `transcript:<harness>:<session>`; there is no
  user dimension to count. It is precisely the multi-user deployments where that half would
  carry the weight, and on a solo install it is permanently 1 by definition — so it cannot be
  validated on the deployment that exists today either.
- `distinct_projects` — `ingest()` writes everything to a single `project_id` (`"core"` by
  default) and clustering runs per project, so the count is permanently 1.

Building the formula on top of that ships a gate that **cannot fire**, which is worse than not
building it: a promotion path nothing can reach reads, from the outside, exactly like one
nothing has yet earned. That is the recurring defect class in this codebase, not a fix for it.

Note also that `MemoryShard.scope` is already taken — it means `global|item`, not a ladder
rung — so this needs its own column plus a migration, and retrieval widening in both
`list_shards` and the raw-SQL path in `search_memory`. `_includes_global` already models a
similar "should this project see shards it did not create" question and is the place to look
first.

The grilled design above is unchanged and stays the plan. What it waits on is tracked as
**GRPH-356**: per-project transcript attribution (the Claude Code adapter can already see
which repo each transcript directory belongs to, since transcripts live one directory per repo
path) and a user dimension on mined evidence. The universal rung remains deferred to the
cloud-tenant design either way.

**Authority.** Night School's install path refuses to write to human-owned artifacts
(propose-only, `core/artifact_adapter.py:135-149`), refuses any target inside its own package
(`_refuses_self_target`), and gates real-harness writes behind an explicit
`--allow-real-install` flag. That maps directly onto design philosophy §2 — capability and
authority as separate contracts — and must survive the port intact. A generated-artifact
engine that can silently rewrite a human's config is the failure mode this whole design
exists to prevent.

**Measurement gap.** The retire path is only as good as the usage signal. In Graphban the
equivalent signal is MCP call metering, which is *better* than transcript scraping — it is
first-party and already counted per tool. Worth exploiting rather than porting
`extract_usage_signal` verbatim.

## 6. Recommended sequencing

1. **Ingest + scrubbing** (one change). Gives Graphban the ability to learn from what
   happened, not just what was written down. Self-contained, no overlap, independently
   useful even if nothing downstream ships.
2. **Promotion ladder.** Extend `_score_shard` with distinct-source recurrence and a decay
   clock. No new scorer.
3. **Artifact engine.** Tier classification → drafting → propose-only install → usage and
   retirement. This is the step that turns *self-documenting* into *self-improving*.

Each phase is independently shippable and independently valuable. Stopping after 1 still
leaves the platform better off.

---

**Tracked as:** GRPH-P16 — *Platform learning loop: transcript ingest, promotion ladder, artifact engine*
