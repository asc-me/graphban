# GRPH-P67 — design revision prompts (2026-11-11)

Fixes for the `graphban-local` designs as of the 2026-11-11 download: `Quick Start v2`,
`Tour Stop` and `Setup Status`. The concepts section is covered by
`prd-67-concepts-prompts.md` and isn't repeated here.

**Workflow:** paste P67 **Part 1** and **Part 2** from `prd-67-guided-setup-prompts.md`, then
**one** revision below. Each revision edits an existing file. It is not a redraw.

## Spec changes these revisions assume

Four things in the designs are beyond P67 v1.0 and are kept on purpose. Before these
revisions count as baseline, the set constraints on **DS-a0e16dd41a** need them, through a
rebaseline of GRPH-P67 (`scope-change`):

1. **Two-pane dialog.** 960px wide: a 380px hero panel on the left, the step body on the
   right. It replaces "560px wide". The hero animation stops under `prefers-reduced-motion`.
2. **Welcome and Sections.** The dialog opens on Welcome, then Sections (Initial setup,
   Graphban concepts, Layout walkthrough). Sections replaces the v1.0 "no checklist hub" line
   for the inside of the dialog only. There is still no `/setup` page.
3. **Graphban concepts**, as specified in `prd-67-concepts-prompts.md`.
4. **Help keeps the guides.** After Finish, Concepts and the tour are reachable from Help.
   This extends v1.0's "Also in Help" from start-up prompts to all three guides.

---

## R1 · Quick Start v2 — Sections and Welcome

> **Revise: Quick Start v2.dc.html, Welcome and Sections.**
>
> - Remove the **NEW** badge on Graphban concepts. A first-run user has seen none of it.
> - Section 3: rename **Layout walkthrough** to **Tour**. The description becomes "Eight
>   stops around the app, ending at Organization. About two minutes." The meta is `~2 MIN`.
>   Choosing it closes the dialog and starts the tour at stop 1, the same tour as Finish →
>   Take the tour. Delete the `NOT DRAWN` walkthrough stub.
> - The Sections description for Concepts becomes "The loop, the tracker, specs, memory, the
>   code graph and rules." Its meta reads `6 CONCEPTS`.
> - Welcome copy stays. Change "come back to any of them from Finish setup" to "come back to
>   any of them from Finish setup, or from Help once you're done."
> - Add one Help frame (toggle `help`): the top-bar Help menu open, with three rows:
>   **Graphban concepts**, **Take the tour**, **Start-up prompts**. Each opens what Sections
>   opens. No other rows.
>
> Toggles: welcome / sections (none done) / sections (setup 2 of 4, concepts done) / help.

## R2 · Quick Start v2 — hero panel states

> **Revise: Quick Start v2.dc.html, hero panel.** The hero follows the three-state rule
> the step rail uses.
>
> - `done`: the path is lit lime, no motion.
> - `pending`: a muted dashed path (`#2a333a`), no moving dot. No amber anywhere.
> - `unknown`: a static grey path (`#7f8891`) with no motion, and the caption ends `· UNKNOWN`.
>   It never plays the flowing animation, which reads as progress.
> - Moving, flowing paths only on a step that is actively waiting on something live: Connect
>   while polling for the first call, First job while counting.
>
> Toggles: one per state on Connect (waiting / connected) and Model (unknown).

## R3 · Tour Stop — org plug copy

> **Revise: Tour Stop.dc.html, org plug.** Restore the spec copy: "**Working with a team?**
> Link this deployment to an org and you get shared house rules (Gitops), memory that
> carries across projects, one roster of everyone's agents, and capacity routing across your
> team's entitlements. Your code graph stays local-first." Keep "deployment": a link key is
> minted per box. Only "(Gitops)" comes back.
>
> In the stop strip, the Specs stop keeps its title. Its body becomes "Versioned specs.
> Draft, grill until approved, then split into tracked items." ("approve" is not a step
> anyone takes; the grill finishing is the approval).
>
> The Tracker stop's body becomes "Six states, as a board or a list. Agents claim ready
> work, and the files an item names — its touchpoints — keep parallel agents from
> colliding." The old "One linear stream, six states, no boards" predates the Board layout,
> which has been the default since GRPH-1144. The Tracker behind the coach mark is drawn in
> the Board layout (six columns, in state order).
>
> The same stale sentence ships today in `web/src/features/tour/stops.ts` (the `tracker`
> stop). That is a product fix, filed as its own item, not part of this design revision.

## R4 · Setup Status — terminal inset

> **Revise: Setup Status.dc.html, terminal inset.** The inset is the `ubuntu-srv` alternate
> from the seed: title `INSET · TERMINAL ON ubuntu-srv`, the line `UNKNOWN report  could not
> report to http://ubuntu-srv:8080: connection refused`, and the caption "The card keeps
> ubuntu-srv at report failed. A failed report is not a report." `build-box` stays
> `never reported` on the card and doesn't appear in the inset.
