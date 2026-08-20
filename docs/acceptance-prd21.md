# PRD-21 acceptance walk — the cloud org plane

D1–D9 are shipped. This walk is the first time anything checks them **against the approved
baseline** rather than against the code that was written, and that distinction turned out to be
the whole point: a review of the merged work found four defects, two of which were sections
whose routes shipped and whose *decisions* did not.

Nothing was red when those landed. Tests passed, CI was green on both engines, and the sections
read as delivered. That is the failure mode this document exists to catch, so run it looking for
things a green suite structurally cannot see.

---

## What the review already found, before this walk

Recorded so the walk does not re-derive them. All four are fixed (PRs #239, #241).

| | Finding |
| --- | --- |
| **GRPH-419** | D8 shipped three routes and left D8.1 behind. `set_member_role` still enforced the retired owner rules, and `test_the_owner_cannot_be_demoted` shipped **green carrying the superseded reasoning in its docstring** — so the correct implementation would have arrived as a red suite and looked like the regression. |
| **GRPH-420** | `memberships` had no `(user_id, project_id)` uniqueness in the model, in migration 0078, or in the live database, while `teams.recompute` read-then-wrote with no lock. A duplicate makes a revocation recompute one row and leave the other granting access. |
| **GRPH-422** | D2 never shipped at all. No `overview` endpoint, `OrganizationView.tsx` untouched by all 87 files, `/org` still the old members page. **G1 was undelivered.** |
| **GRPH-423** | The speculative gate covered the nav item and not the route, so `/org/admin/branding` rendered by URL in a normal build. |

**The pattern worth carrying into the walk:** two of the four were *sections that looked
delivered*. Check the decision, not the diff.

---

## Mechanically verified

Run on `acceptance/prd21` at the merge of #241. Each is a command with one right answer.

| AC | Result | Evidence |
| --- | --- | --- |
| 1 | ✅ | `grep -n activeProjectId web/src/lib/api.ts` → nothing. The explanatory comment was reworded rather than deleted (GRPH-421). |
| 2 | ✅ | `wrong-project-write.test.tsx` — asserts the **request body**, not the rendered list. |
| 3 | ✅ | same file — `githubConnect` targets the project in the request. |
| 4 | ✅ | `hierarchy.test.tsx` — `/p/GRPH/code`, `/p/grph` case-insensitive, non-tags rejected. |
| 5 | ✅ | `lastProjectTag` / `rememberProjectTag` in `routes.ts`. |
| 6 | ✅ | `FlatRedirect` mounted for every `PROJECT_VIEWS` path. |
| 7 | ✅ | `test_org_overview.py` — 404-not-403, plus a **two-org** test that isolates the org filter. |
| 8 | ✅ | `test_the_fail_closed_guard_is_untouched` — `can_read(None)` still False. |
| 9 · 9a · 9b | ✅ | `test_galaxy.py` — evidence required, omitted-vs-empty, `provides` collision draws nothing. |
| 9e | ✅ | `test_teams.py::test_two_sessions_materializing_the_same_pair_cannot_both_win`. |
| 10 · 11 | ✅ | `test_galaxy.py` — staleness on re-push, unresolved names counted not drawn. |
| 12 | ✅ | Three files touch the layout core and only one implements it: `layout.ts`. `layout.worker.ts` runs it, `useGraphLayout.ts` imports it. **One owner.** |
| 13 | ✅ | `test_teams.py` — derived rows carry `origin`, revocation recomputes. |
| 14 | ✅ | `git diff` — `authz.py` untouched across the entire PRD-21 range. `can_read`/`can_write` byte-identical. |
| 15 | ✅ | `test_membership_mutations.py::test_the_last_administrator_cannot_be_demoted_or_removed`, both directions. |
| 16 | ✅ | `deployments.test.tsx` — the missing-address state. |
| 17 | ✅ | `self-host-routes.test.tsx` — no org route registered before the hosted gate. |
| 18 | ✅ | `test_cross_tenant.py` covers overview, galaxy and teams. Green on SQLite and Postgres in CI. |

Backend **1982 passed**, 18 skipped. Web **292 passed**, typecheck clean.

### Three criteria had no test at all, and the walk is how that surfaced

AC 2, AC 3 and AC 17 were **specified and never written**. Nothing was failing — there was
simply nothing asserting them, which is exactly the shape of absence a green suite reports as
success.

AC 2 is the sharpest. The existing `hierarchy.test.tsx` asserts that `ProjectContext` resolves
the right project from the route, which is necessary and not sufficient: **the defect P0 removed
was a write, not a render.** A test on the rendered list would pass on the buggy code, because
the list re-fetches once the effect settles and papers over a row already written to the wrong
project. The new tests assert what leaves the client.

Every new test here was sabotage-checked. Ungating an org route fails AC 17; dropping the org
filter fails AC 7's two-org case; removing the membership constraint fails AC 9e.

---

## What still needs a human

Six things a test cannot reach. Predictions are recorded **before** running, so a comfortable
result is not mistaken for a verified one.

**Setup:** `cloud.graphban.dev` (hosted, `hosted_mode: true`) with a linked box pushing to it.
A scratch stack cannot produce the states steps 3 and 5 exist to surface.

1. **Deep-link cold.** Open `/p/GRPH/code` in a browser with no session, log in, land on that
   project's graph — not `projects[0]`. *Prediction: passes. The risk is the post-login redirect
   dropping the intended path, which no test covers.*
2. **Close and reopen the browser.** Last-used should survive. *Prediction: passes; `localStorage`
   is the mechanism and it is asserted in isolation.*
3. **A deployment on a LAN address, viewed from elsewhere.** The address must render as legible
   text and the rest of the screen must be complete. *Prediction: passes — but this is the one I
   would bet against, because "complete without the link" is a judgement no assertion makes.*
4. **Remove a dependency from a real manifest and re-push.** The edge should fade, keep its
   evidence, and say how long ago it was last seen. *Prediction: passes. Watch that the count in
   the push response matches what the galaxy then shows.*
5. **An org whose only administrator tries to leave.** The refusal must name what would be lost
   and the suggested sequence must actually work. *Prediction: passes; asserted in both
   directions.*
6. **Look at `/org` as somebody who has never seen it.** Does it answer "how are my projects
   doing?" in one glance? *No prediction. This is the only step here that can fail without
   anything being broken, and it is the reason the section exists.*

---

## Known disagreement, deliberately unresolved

`/org/integrations` is **not** gated, and D9 says it should be. `OrgIntegrations` is a
connector × project matrix in which GitHub and Google Drive are genuinely backed by
`routers/platform.py`; only Jira, Linear, Confluence and Trello carry the speculative chip.

So **D9's premise is factually wrong** — integrations is not entirely PRD-23 territory. Hiding a
working screen to satisfy a mistaken sentence is the wrong repair, and amending an approved PRD
is the author's call. The code and the spec are left disagreeing on purpose, and this note is
here so the next reviewer does not file it a third time.
