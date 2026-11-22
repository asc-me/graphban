---
name: graphban-grill
description: >
  Settle a Graphban PRD grill from the product why and the PRD's intent.
  Auto-record answers the context already decides, and ask the user only
  the questions it does not. Use when grilling a PRD, running a grill,
  answer_grill, or when the user runs /graphban-grill.
user-invocable: true
---

# Grill

`grill_prd` returns questions and records nothing. An answer counts only after `answer_grill`. The grader ignores the PRD body: a decision that exists only in the document is still unanswered.

## Sources

Read both before you classify a question.

1. `get_prd`. The intent is the framing: Problem, Goals, Non-goals, and an intent heading when the PRD has one.
2. Why the app exists: `docs/product-overview.md` (philosophy, goals, non-goals) and `docs/design-philosophy.md`. This bounds the PRD. It does not fill a hole the PRD left open.

A source decides a question only when you can quote the sentence. Memory, code, and a guess are not sources.

## Split

For each question, write a one- or two-sentence recommendation, then:

- **accept** — that sentence already decides it, the two sources do not conflict, and the answer is words rather than a prototype. Record it.
- **ask** — the sources are silent, they conflict, both readings are reasonable, or the question needs a prototype. These are the only questions you put to the user.

Silence is ask. A fluent inference is ask. Do not accept a new boundary, a new error behavior, or a new contract field that neither source states.

## Record an accept

Fold the decision into the PRD with `update_prd(section=...)` first. Then `answer_grill` once, with that decision alone.

One call per decision. A reply that covers several dimensions is one answer, and the grader leaves the rest outstanding.

Say `deferred` only when a source already defers it. "Later" without that word stays unanswered.

Read the return:

- `graded: false` — stop. Say `ungraded_reason`. Do not send another answer.
- `stalled: true` — stop. Tell the user. Three answers moved nothing.
- `complete: true` — the grill finished and the body just froze. The fold had to happen before this call.
- `body_absorbed: false` — the transcript is ahead of the spec. Fold the answers it names before you continue.

Do not set `approved`. Finishing the grill does that.

## Ask

Show only the ask list. For each: the question, the recommendation, and one line on why the sources do not decide it.

Record nothing from this list until the user answers. `answer_grill` with their words. Fold that section the same way, before the next call.

If they accept a recommendation, the recorded answer is their acceptance of that text. The recommendation alone is not a turn.
