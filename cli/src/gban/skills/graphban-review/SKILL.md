---
name: graphban-review
description: >
  Review a Graphban item you did not build: claim_review, sign_off, or bounce.
  Use when reviewing, signing off, or bouncing an item, or when the user runs
  /graphban-review.
user-invocable: true
---

# Review

Review is a second person. `claim_review` refuses the builder.

1. `claim_review`. `claimed: false` means the item is yours, or it needs `gh` or `human-only` you did not declare. Stop on that item.
2. The diff is the `branch` on the claim. Read that branch. A review does not orient the whole repo.
3. `sign_off` needs `evidence` and the `commit` SHA you reviewed. It refuses the builder, and it refuses the delegation's `delegated_by`. Above the effort bar it also refuses without a `sabotage` receipt: something broken on purpose and caught by a test. `tests_failed` of zero means the test cannot fail.
4. `waive` is `{predicate: reason}` for a check that could not run. A check that ran and failed stays failed. The field does not clear it.
5. `bounce(id, reason)` sends it to `next`. The author keeps the worktree for one lease, then the fleet can take it. The person who delegated the item may bounce it. They may not sign it off.
6. When the item cites a PRD, call `prd_acceptance` with one `view`. Those views describe what happened. They do not report complete. `get_prd` for the whole body is an edit, not a review. Authoring is `graphban-prd`.

`release_item` on a review claim leaves the item in `review`. A bounce is `bounce`.
