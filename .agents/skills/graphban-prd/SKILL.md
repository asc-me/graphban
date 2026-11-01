---
name: graphban-prd
description: >
  Author, grill, decompose, or close a Graphban PRD. Use when writing a spec,
  running a grill, answer_grill, update_prd, decompose_prd, request_rebaseline,
  close_prd, or submit_verdict, or when the user runs /graphban-prd.
user-invocable: true
---

# PRDs

Approval is the grill finishing. `update_prd` cannot set `approved`.

Read with one call. `get_prd` when you will edit. `prd_coverage` for gaps: `shaped: false` is not a clean pass, `gaps` means no tasks, `empty_sections` means no substance. `prd_acceptance` with one `view` for delivery. `completeness` is the view that lists baselined sections with nothing delivered.

## Grill

`grill_prd` returns the next questions and records nothing. Ask the author. `answer_grill` with their words. `graded: false` means the grader was not asked and this answer was not judged. Call `grill_prd` again while `outstanding` remains. `complete` is the grill finishing, which is what reaches approved.

Recording an answer with `update_prd` replaces markdown and leaves the PRD in `draft`, with no turns and no dimensions.

## Edit

`update_prd(section=<the ## heading>, body=<that section>)` rewrites one section. A whole-body replace needs `base_hash` from `get_prd`. A moved hash is a refusal. `status` on this call is `draft` or `review`.

## Decompose

`decompose_prd` with `create` false first. One proposed task per uncovered section. The task carries the section's framing, so the worker does not also read the PRD. Framing sections are tasks only when `include_prose` is true. `create` true files them once the grill has approved the PRD.

## Rebaseline

`request_rebaseline(reason_type, reason)` in your own words. `reason_type` is `learning`, `scope-change`, or `correction`. It reopens the grill. The current baseline governs until a new one is earned. Adding a section is a new PRD. A closed PRD is refused.

## Close

`close_prd` is terminal. Every section that `completeness` says has nothing delivered appears once in `dispositions`, as `promoted` (`promote_to` `item` or `prd`) or `deferred` with a reason. After close, further work is a new PRD.

## Verdict

`submit_verdict` citations are `{kind, ref}` and must resolve. `kind` is `code` (a code-graph path), `intent` (a baseline section — what an absence finding cites), or `evidence` (an item key). A citation that does not resolve is rejected. Signing work you claimed is flagged.
