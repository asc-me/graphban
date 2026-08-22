# Where an agent's tokens actually go

GRPH-462. One session was instrumented on 2026-08-21 and the result contradicted two
things everyone assumes. This re-runs it over **20 sessions across 8 repositories**, with
a classifier anyone can read, disagree with, and re-run.

```bash
scripts/token_census.py --corpus 2        # every transcript ≥2MB, deterministic order
scripts/token_census.py --classifier      # the bucketing rules
scripts/token_census.py --sample source   # audit what actually landed in a bucket
```

## The finding holds

**Source inspection is 55.5% of all tool-result tokens** — 2,685,099 of 4,835,360 across
23,756 calls. The single-session measurement said 59.3%. It is the **top bucket in every
one of the 20 sessions**, ranging 37% to 75%.

| kind | tokens | share | calls | per call |
|---|---:|---:|---:|---:|
| source | 2,685,099 | **55.5%** | 6,151 | 436 |
| ledger (MCP) | 625,492 | 12.9% | 1,909 | 327 |
| shell_other | 356,427 | 7.4% | 2,782 | 128 |
| test | 285,026 | 5.9% | 2,991 | 95 |
| write | 224,217 | 4.6% | 5,085 | 44 |
| git (plumbing) | 160,534 | 3.3% | 1,340 | 119 |
| mcp_other | 122,070 | 2.5% | 790 | 154 |
| other | 96,579 | 2.0% | 585 | 165 |
| remote (ssh/docker) | 90,778 | 1.9% | 635 | 142 |
| network | 88,898 | 1.8% | 507 | 175 |
| git (reading history) | 79,981 | 1.7% | 212 | 377 |
| agent, todo | 20,259 | 0.4% | 769 | — |

Both counter-findings survive, one of them differently.

**"Agents waste tokens re-reading the same things" — still false.** Of 6,151 source
inspections, **216 were exact repeats (3.5%)**, worth 5.8% of all tokens. The
single-session figure was 0.5%; over twenty sessions it is seven times that, and still
small. Meanwhile 1,352 calls (22.0%) hit a target already seen **with a different
question** — which is how a large file gets read correctly. Those two numbers must never
be added: *"22% of looks were re-looks"* and *"3.5% were exact repeats"* describe the same
data and say opposite things.

**"Verification dominates the burn" — still false.** Test runs are **5.9%**, on 2,991
calls, at 95 tokens each against source's 436, because output is already piped through
`tail`/`grep`.

## The number that was not in the original, and matters more

**The biggest tenth of answers carry 46.7% of all source tokens.**

p50 **222** · p90 962 · p99 3,724 · max 11,698 · mean 437. 1,850 answers came back under
100 tokens; 192 came back over 2,000.

The cost is **concentrated, not spread**. Half of all source spend sits in roughly 615
calls out of 6,151. So a retrieval layer does not have to replace how an agent reads — it
has to replace the fat tail. That is a much smaller thing to build and a much sharper
target, and the mean (437, sitting between p50 and p90) hides it completely.

## What nearly went wrong, and why the classifier is the deliverable

The first version reported source inspection at **35.9%** and put **30.8% of all tokens
in its residual bucket**. Auditing that bucket with `--sample shell_other` showed it was
almost entirely `cd <repo> && <the real command>`: every rule anchored on `^`, so the
most common shape in these transcripts fell straight through.

Stripping navigation prefixes moved the headline from 35.9% to 55.5% and settled the
question the other way. **A twenty-point swing on a regex detail is not a measurement**,
so the prefix stripping has its own tests — including a control asserting `cd /repo &&
pytest` is still `test`, because a fix that made everything `source` would be
manufacturing the conclusion rather than measuring it.

Three buckets exist specifically so they cannot settle the question by definition:

- **`git_read`** (`git show`, `git log -p`) returns code. Calling it source inflates the
  thesis; calling it plumbing hides real reading. 1.7% either way.
- **`remote`** (`ssh host 'grep …'`) inspects a *deployed instance*, not the codebase.
- **`ledger`** is separated from other MCP because it is the thing being evaluated. If
  source inspection dominates, the pitch is that the ledger absorbs some of it — so its
  **12.9% is the baseline any proposal has to beat**.

Also corrected mid-analysis: two runs appeared to show reclassification changing the
*total*, which is impossible — reclassification moves tokens between buckets. They were
different corpora; `find | head -14` and a sorted glob picked different files. `--corpus`
now selects deterministically, because comparing two corpora and calling it a
before/after is an easy way to be confidently wrong.

## Method, and its limits

- Every `tool_result` is attributed to its `tool_use` by id. Unmatched results are
  dropped rather than guessed at.
- Tokens are `len(text) // 4`. No offline tokenizer exists, and this is the estimate
  `test_mcp_footprint.py` already uses, so the numbers are comparable to the manifest
  budget this repo reasons about. Everything here is a ratio; a constant factor does not
  move it.
- A call's *target* comes from `file_path`/`pattern`, or the first path-shaped token in
  the command. Calls with no recoverable target are **excluded** from same-target counts
  rather than guessed at — a bad guess there manufactures exactly the re-reading
  conclusion this disputes.
- **The corpus includes the session that produced it**, which was still being written
  during analysis: two runs minutes apart differ by a few calls. Immaterial at this
  scale, and worth knowing before someone re-runs it and gets different digits.
- **These are one person's sessions.** Twenty of them across eight repositories is enough
  to kill "it was one unusual session", and not enough to claim anything about agents in
  general.

## What this gates

GRPH-462 was filed as a gate: if source inspection is not the dominant cost across
sessions, GRPH-463 and GRPH-464 are wrong and get closed unbuilt.

**It is dominant, so the gate opens** — with one amendment to the thesis. The target is
not "agents read too much": they mostly read narrowly and correctly, and a perfect cache
would recover under 6%. The target is the fat tail — **615 calls carrying half the
cost**. Any retrieval contract designed from here should be judged on whether it replaces
those, not on how many lookups it intercepts.
