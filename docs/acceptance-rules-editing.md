# PRD-82 rules editing — acceptance walk (GRPH-1602)

Walked on the real box: `ubuntu-srv:8080`, project `p74-walk` (P74W), rule pack 0.6.0 (profiles v0.3.0).

- **Releases:** steps 1 and 3 ran on 2026.11.39–11.45. Steps 2, 3b and 4 ran on 2026.11.46 (`d851f9e6`), checked against `/health`.
- **How the rules were changed:** only on the Rules page. Readback used `get_rule_toggles` (read-only), the page's own History panel, and the Activity page.
- **Who drove the page:**
  - The person drove step 1 and the first Quick configure applies.
  - At the person's request, a planner session drove their signed-in browser for the step 3 drift change, step 2 and step 4. It used the same page with no API calls.

## Summary

| # | Step | Verdict |
|---|---|---|
| 1 | Resume GRPH-1431: reset the API-set rules and set them again on the page | **pass**, after fixes GRPH-1727 and GRPH-1742; see [acceptance-prd74.md](acceptance-prd74.md) for the drain and done gate |
| 2 | Set the test-scope mode only; engines survive | **pass** |
| 3a | Quick configure: apply Regulated with one exception; show the chip, the per-rule history and one `apply_rule_profile` event | **pass**: one event, `exceptions: review.distinct_vendor`; GRPH-1756 (exception control) and GRPH-1803 (11.45 false drift) fixed along the way |
| 3b | Change a rule by hand and show drift | **pass**; found GRPH-1806 |
| 4a | Wizard: Next through every step changes nothing | **pass** |
| 4b | Wizard: set two rules and show both saved with their reasons | **pass, with one deviation**: one shared reason, not one per rule |

## 1. Resume GRPH-1431 on the page

The person reset each rule that had been set through the API on 2026-11-13, then set it again in the stage editor. Read back with source `project`:

- exclusive areas `["db/migrations"]`;
- generated files: the 9 defaults plus `client/generated/`;
- done predicates `["suite_green", "scans_clean"]`. `suite_green` shows as a fixed floor row.

**Found and fixed during this step:**
- **GRPH-1727:** "Reset to inherited" kept the saved one-entry list instead of the 9 defaults, and the next edit quietly undid the reset.
- **GRPH-1742:** exclusive areas did not join two items that edit different files under one prefix.
- **GRPH-1740:** the generated-files radios did not use the card style.
- **GRPH-1741:** the Why field did not say it is required.
- **GRPH-1728 and GRPH-1729:** the map strip clipped, and the docs bubble covered Save.

All of these shipped by 2026.11.40. The drain and the done-gate check that followed are recorded in [acceptance-prd74.md](acceptance-prd74.md).

## 2. Test-scope mode only, engines survive

On p74-walk (Build → "Where the test suites run"):

1. Engines were changed from `[postgres, sqlite]` to `[mysql]` and saved with a reason. Toast: "engines postgres, sqlite → mysql". Read back as `{mode: agent_and_pr, engines: [mysql]}`, source `project`.
2. Only the mode was changed, to "The agent runs partial CI" (`agent_targeted`). The draft listed a single change, `mode agent_and_pr → agent_targeted`. It was tagged WEAKENING, and the risk tick was required. Saved with a reason.
3. Read back as `{"mode": "agent_targeted", "engines": ["mysql"]}`. The engines survived a write that did not name them.

## 3. Quick configure: apply, then drift

**Apply.** Regulated v0.3.0 was applied with one exception, `review.distinct_vendor` (Reviewer is a different vendor).

**The `apply_rule_profile` events** (Activity page, P74W, 24h range; read 2026-11-21, oldest first). Every row is attributed to alex, on surface `rest`:

| Apply | Reason | `written` | `exceptions` |
|---|---|---|---|
| Standard 0.3.0 | "applying Standard 0.3.0: losening review standard" | `merge.done_predicates`, `review.sabotage_required` | `review.distinct_vendor` |
| Standard 0.3.0 | "applying Standard 0.3.0: set to standard" | `review.distinct_vendor` | none |
| **Regulated 0.3.0** | "applying Regulated 0.3.0: regulated but 1 expc" | `merge.done_predicates`, `review.sabotage_required` | **`review.distinct_vendor`** |

- **One event for the Regulated apply.** It is a single `apply_rule_profile` row: `profile: regulated · profile_version: 0.3.0 · pack_version: 0.6.0`. It lists the 10 rules it names, and its `exceptions` is `review.distinct_vendor`.
- **Only the differing rules were written.** That apply wrote two rules, and each has its own `set_rule_toggle` row carrying the same reason:
  - `merge.done_predicates`: none → `suite_green, scans_clean`;
  - `review.sabotage_required`: `min_tests_failed` 1 → 2.
- **The exception was not written.** `distinct_vendor` got no row from this apply, and stayed off as the second Standard apply had left it.
- **Per-rule history matches** (rule dock → HISTORY). For done predicates: "p74-walk: done also needs clean scans" → cleared by "losening review standard" → back to `suite_green, scans_clean` by "regulated but 1 expc".
- **What the person intended:** they had meant to keep done predicates as the exception. It was not kept: Standard had cleared it, and Regulated wrote it back.

**Found during this step:**
- **GRPH-1756:** the only way to set an exception was a faint "Drop" link in the review sheet. Quick configure now has a "Keep current (exception)" control on each changing row, with Restore.
- **An exception was lost:** on the first attempt the person picked a level while already on Compare. That cleared the kept rows, so the apply went out with no exception. This is the same `choose()` reset as GRPH-1806 below. The second attempt recorded one exception.
- **GRPH-1803:** on 2026.11.45 the chip read "drifted" right after a clean apply. The rule pack's `accept.human_clause_blocks` template said `stage = "Spec"`, but its primitive was registered for Review. The library marked the rule unmeasured, and drift counted it. CI missed this because its fixture used the old stage. Fixed in 2026.11.46.

**On 2026.11.46,** before any hand change: the chip read "Regulated v0.3.0 · 1 exception", with no drift.

**Drift.** In Review → "A noted gap blocks sign-off", the rule was turned Off and saved with the reason "P74 walk: hand change to show drift from Regulated":
- The chip changed to "Regulated v0.3.0 · 1 exception · drifted (1)".
- In Compare, that row reads "CHANGED BY HAND · WEAKENING" (this project: off, Regulated: on).
- The rule's resolution shows PROJECT off IN FORCE, with the platform default overridden.

**Defect found: GRPH-1806.** Compare for the applied level lists the existing exception, `distinct_vendor`, as a rule that "would change", and offers "Keep current (exception)" again. `choose()` starts with no kept rows, and only the pack-update path carries the last apply's exceptions. Re-applying Regulated to clear drift would therefore silently drop the recorded exception. This was filed as GRPH-1806 on the maintenance line.

## 4. Wizard

**Next through every step.** In "Set up rules" the steps are Plan, Build, Review, Merge and Learn. Spec and Release are listed as skipped, each with its reason. Every question shows "Nothing detected" and starts on "Keep the default". Clicking Next through all five and then Review shows:

> Every answer is "Keep the default", so there is nothing to apply.

Nothing was written: the chip and `get_rule_toggles` were unchanged.

**Set two rules.**
- **Plan → Exclusive file areas → Set my own:** added `db/seeds`. The editor said "can't count": "The code graph can't count files for this project, so no counts are shown". It did not show 0. It also previewed "2 open items would be serialised: P74W-1, P74W-2".
- **Build → Where the test suites run → Set my own:** mode back to `agent_and_pr`.

The Apply sheet showed "Review 2 changes":
- `paths: db/migrations → db/migrations, db/seeds`, with the note "P74W-1 and P74W-2 would build one at a time";
- `mode: agent_targeted → agent_and_pr`.

Each change showed `source: project → project`. Both were saved with one reason, "P74 walk: wizard sets two rules (seeds exclusive, CI runs again)". The page landed on the map, and drift fell from 2 to 1.

Read back:
- `plan.exclusive_areas` `{"paths": ["db/migrations", "db/seeds"]}`;
- `build.test_scope` `{"mode": "agent_and_pr", "engines": ["mysql"]}`.

The engines survived this write too.

**Deviation.**
- **One reason, not one per rule.** GRPH-P82's wizard section says the review takes "a reason per rule". The sheet asks for one reason that "covers every rule above". This matches the PRD's stage-editor decision, but not the wizard text. It is recorded here and not filed. The PRD contradicts itself, so which one is right is the PRD owner's call.
- **No visible highlight.** After Apply, the changed rules sat inside "+N more" on their stage cards, and no highlight was visible on the map.

The hand change and the wizard's two saves appear on the same Activity page as `set_rule_toggle` rows, with their reasons and before → after params. The wizard's two rows share one reason.

## Open after the walk

- **GRPH-1806** (re-applying the applied level drops its exceptions) and **GRPH-1804** (an unmeasured rule labelled "CHANGED BY HAND") were found on this walk and merged to main afterwards, as #2034 and #2019. Neither is in 2026.11.46, so neither was seen on the box.
- **The two P74 gaps:** CI attestation from a LAN box, and a check of an undeclared generated file. See [acceptance-prd74.md](acceptance-prd74.md).
