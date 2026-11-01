---
name: graphban-answer
description: >
  Answer a question from the session, the PRD, the product why, published
  memory, and a related decision that already settled the same act. Ask the
  user only when those sources do not decide. Use when a wait:decision is
  open, before asking the user a mid-build question, during a wave or spawn,
  or when the user runs /graphban-answer. Grill questions stay on
  /graphban-grill.
user-invocable: true
---

# Answer

You are the session that launched the work. A child does not run this. `gbfleet until` does not run this. `watch-wave` reports and leaves every wait blocked.

Grill questions are `graphban-grill`. Do not `answer_grill`. Do not `update_prd`. Lane, tier, and harness stay where they were chosen.

Pass `project_id` on `search_items` and `search_memory`. A key that can see several projects writes to the wrong one when you omit it.

## When

In this session, before you ask the user.

A drain or a spawn: in the same turn `gbfleet service install` reports the drain running, or the first `spawn` returns an agent id, attach a scheduler beside `watch-wave`. `scheduler_list` first. One loop per project.

`scheduler_create` with `interval="5m"`, `fire_immediately=true`, `durable=false`. The stored prompt stands alone.

```
You answer Graphban wait:decision items for project PROJECT. This prompt is self-contained. Follow the graphban-answer skill if you can read it. If you cannot, do only what is written here.

PRD scope, when the drain has one: PRD. Skip a wait that is not on that PRD.

search_items status=blocked tags=["wait:decision"] project_id=PROJECT.
get_item_details on each id. A description with no question: print the id and leave it blocked.

Sources, in order. A later source does not override an earlier one. Quote the sentence or leave the wait blocked.
1. get_prd. Framing only: Problem, Goals, Non-goals, an intent heading, Decisions from grilling. docs/product-overview.md and docs/design-philosophy.md bound the PRD. They do not fill a hole the PRD left open.
2. search_memory for the question, project_id=PROJECT. Leave include_candidates off. The shard text must be the decision. A score is not a vote.
3. A related decision that already settled the same act, in one step. search_items status=done tags=["wait:decision"] project_id=PROJECT, keep this PRD, and read the evidence note. related_work on the original, then get_item_details on a done wait:decision it names. Quote that decision and name the one step. A longer chain is ask. A non-goal that refuses to choose is not a decision.

Leave wait:secret, wait:access, wait:deploy, and wait:merge blocked. Name them.

A quote: evidence on the wait, kind note, the sentence and where it came from. update_item status=done on the wait only.
Do not update_prd. Do not answer_grill. Do not move the original item. Do not publish_memory.

No quote: print the question, your recommendation, and one line on why the sources do not decide. Leave the wait blocked.

Do not spawn, merge, sign_off, bounce, or claim.
```

Substitute PROJECT and PRD. No placeholders left in the stored prompt.

## Sources

Read these before you classify. A source decides only when you can quote the sentence. A later source does not override an earlier one.

1. A sentence the user already said in this session. A scheduler fire has no session. Absence of that sentence decides nothing.
2. `get_prd` for the item's PRD. Framing only: Problem, Goals, Non-goals, an intent heading, Decisions from grilling. `docs/product-overview.md` and `docs/design-philosophy.md` bound the PRD. They do not fill a hole the PRD left open.
3. `search_memory`. Leave `include_candidates` off. A shard counts when its text is the decision. A lesson about a mechanism does not. Do not call the decider. A score is not a vote.
4. A related decision that already settled the same act. Quote it, and name the one step that makes this question that act. Look here:
   - `related_work` on the original, then `get_item_details` on a hit that is a done `wait:decision`. The evidence note is the decision.
   - `search_items(status=done, tags=["wait:decision"])` for this project. Keep a row whose `prd_id` matches. The evidence note is the decision.

One step is the same actor doing the same thing to the same kind of thing. "The wave does not retry a failed child" decides "should the wave start a failed reviewer again", because a reviewer is a child and starting it again is a retry. It does not decide "should a person be paged" or "should the retry be billed."

A longer chain than one step is ask. Two related decisions that disagree are ask. A non-goal that says the work does not choose is still ask. The product why still does not fill a hole the PRD left open.

Code says what the repo does. It is not a source.

Silence is ask. A conflict is ask. Two reasonable readings are ask. A new contract or a new error behavior that no recorded decision states is ask.

## Leave blocked

`wait:secret`, `wait:access`, `wait:deploy`, and `wait:merge`. Gitops, sign-off, and spend. Name the item. Leave it blocked.

## The wait

`search_items(status=blocked, tags=["wait:decision"])`.

`get_item_details`. The description is the question and what each answer would make the child do. A title and no question is ask. Say the wait has no question.

A `--prd` drain answers a wait whose `prd_id` matches, or whose dependency link has `b` this wait and `a` an item on that PRD. Leave every other wait.

The original is `a` on that dependency link. You are not its builder. Leave it blocked. Marking the wait `done` returns it to `next` when no other unmet dependency remains.

## Quote

The answer is the quoted sentence and where it came from. When the quote is a related decision, the note also names the one step. `update_item` on the wait: evidence `kind` `note` with that sentence, then `status=done`.

A decision that should outlive the item: `add_memory`, scope `global`. Read the returned `status`. Leave a `candidate`. Do not `publish_memory`.

## Ask

One question. The question, your recommendation, and one line on why the sources do not decide it.

Record nothing until the user answers. Their words are the evidence, then the wait is `done`. The recommendation alone is not the answer.

A scheduler fire that must ask prints that question and leaves the wait blocked. The parent session puts it to the user.

## Stop

`scheduler_delete` when the user says stop, when the drain is gone, or when no `wait:decision` remains. A secret, access, deploy, or merge wait stays. Name it.
