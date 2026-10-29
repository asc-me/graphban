# Rules view — design contract (GRPH-P61)

Source of truth for the pixels: `docs/design/Rules.dc.html` (every state, driven by query
params) and `docs/design/Rules - Hi-fi.dc.html` (the reviewed board: 36 frames, 1a–7c, each an
iframe of `Rules.dc.html` in one state). `Rule Level Icon.dc.html` is the enforcement-level
glyph. Exported from Claude Design project **graphban-local** on 2026-10-28 (revision 1) and
reviewed against the approved PRD and the backend. This markdown is the checklist; the
`.dc.html` is the look. **Where they disagree, this file and GRPH-P61 win.**

The spec is **GRPH-P61** (approved v1.0 after the rebaseline grill). Read §3–§6 before building.

## Read this first

- **The mock hardcodes its data; the view must not.** `Rules.dc.html` carries a 20-rule seed,
  fixed stage coordinates and fake counts. The real view renders whatever the registry route
  returns (§3 of the PRD), computes stage state from it, and lays out from it.
- **The point of the view is that a drawn rule never reads as an enforced one.** Every
  enforcement word on screen must be true of the running server. If a value cannot be read,
  the view says so — "no result recorded", "not connected", "org unreachable" — never blank,
  never green.

## Enforcement levels (match the mock exactly)

| level | word | treatment in the mock | rank |
|---|---|---|---|
| enforced | ENFORCED | lime, solid 1px | 3 |
| checked | CHECKED | blue, solid | 2 |
| external | EXTERNAL | purple, **double** border, "NOT VERIFIED" box on the row | 1.5 |
| advisory | ADVISORY | amber, dashed | 1 |
| unmeasured | UNMEASURED | red, dotted, hatched fill | 0 |
| not used | NOT USED | grey, dashed, dimmed | — (excluded) |

Icon **and** word everywhere, never colour alone. A stage is drawn at the lowest rank among its
*used* rules; NOT USED only when every rule on it is not used. The legend is always visible and
counts by level.

## Overrides to the export (already patched into the files in `docs/design/`)

These were found in review and corrected in the committed `.dc.html`; they are listed so the
build does not reintroduce them from memory of the export.

1. **`merge.ci_green` says where its result comes from.** Title "Merged work passed CI";
   summary "CI attests suite_green on each item's branch; this reads the latest attestation on
   a merged item."; result "PASS · last attested 2h ago". The server holds `suite_green`
   attestations (`items.CI_PREDICATE`), not GitHub Actions runs — never word it as "the latest
   CI run".
2. **Recommended-rule chips do not claim enforcement that doesn't exist yet.** A value
   suggestion reads `WOULD BE <LEVEL>` (always ADVISORY for gitops); a code suggestion reads
   `<LEVEL> ONCE BUILT`, next to "Needs a code change. Until it ships, nothing holds it."
3. **Naming tokens are only `{item_id}` `{tag}` `{slug}` `{version}` `{date}`.** Examples and
   captions use `{tag}-{item_id}: {slug}`. The editor validates against this list
   (`gitops.NAMING_TOKENS`) and shows the server's 422 verbatim.
4. **Skills link to the pack gban ships, at its tag.** The nine skills in
   `cli/src/gban/skills/` at `cli-v0.8.2` (graphban-delegation, -grill, -orient, -prd, -review,
   -scan, -wave, -work, watch-wave), linked as
   `github.com/asc-me/graphban/blob/cli-v0.8.2/cli/src/gban/skills/<name>/SKILL.md`. The
   sabotage rule's enforcer links to the same path for `graphban-review`. The real view takes
   the version from the running gban/package metadata, not a literal.
5. **The Recommended panel kicker reads "SUGGESTED · FROM THE SOURCES BELOW"** so it stays
   true when GitHub is not connected (5e).

## Editing in place

Rules is an editing surface, not just a map. `db`-truth rows edit inline; everything else is
read-only text with its enforcer link.

**Gitops fields** (Merge, Build, Review, Release stages):
- A wrapper in `features/rules/` renders `GitopsPanel`'s field editors **only when
  `control.writable` is true**; otherwise read-only text, no greyed inputs. `GitopsPanel` is not
  changed.
- Save is `PATCH /api/projects/{id}/gitops`. On success the row goes UNMEASURED → **ADVISORY**
  ("Told to agents in every get_context. Nothing refuses a violation.") and the stage pill
  recomputes. Never ENFORCED.
- `linked_set` / `linked_unset`: no editor, "Inherited from <org>" / "Your org has not set
  this", link to Admin → Gitops. `linked_unreachable`: every gitops row UNMEASURED under the
  amber banner, no values, no inputs.
- Read-only copy follows `gitops.writable_for`: org project → "Only an org owner or admin can
  change this"; otherwise "You don't have write access to this project".

**Budget cap** (`plan.budget_cap`, Plan stage, frame 2e) — **edited inline here** (decided
2026-10-28):
- Read with `GET /api/fleet/policy?project_id=…`; write with `PUT /api/fleet/policy`
  (`routers/fleet.py`), guarded by `authz.require_writable`. The write is recorded as
  `set_fleet_policy` in the ledger.
- **`PUT /api/fleet/policy` replaces the whole policy.** The editor must read the current
  policy and send every field back (`local_only`, `reviewer_cross_vendor`,
  `allowed_harnesses`, `caps`) with only the edited cap changed — sending `{caps: …}` alone
  resets the others to their defaults. Cap keys are `per_attempt_tokens`, `per_item_tokens`,
  `per_period_tokens`, `period` (`fleet_profiles.CAP_KEYS`); the row edits `per_item_tokens`.
- Known gap: `PolicyIn` has no `cluster_ceiling`, though `normalise_policy` stores one, so any
  PUT through this route drops a stored ceiling. Nothing in the app sets a ceiling today; fix
  `PolicyIn` (or add a merge-style write) before this editor ships, and test that a cap edit
  leaves every other policy key unchanged.
- The rule stays **EXTERNAL** after a save: Graphban stores the cap; the gbfleet supervisor
  enforces it, and some vendor rows can't honour one. The "NOT VERIFIED" box stays.
- A 422 (`ProfileInvalid`) is shown verbatim and the typed value is kept.

## Frames to match (from the Hi-fi board)

1a–1d map (fit, hover, keyboard focus, low zoom) · 2a–2e inspector (Review, Merge, Learn, no
rules registered, Plan) · 3a1–3f editing (clean, dirty, saving, failed, saved, linked_set,
linked_unset, unreachable, no write, saved with canvas) · 4a1–4b2 following a refusal (mapped,
unmapped) · 5a–5e unmapped refusals (populated, none, error) and Recommended (connected, GitHub
not connected) · 6a–6e edge states (push_to_base, fresh, loading, error, 820px) · 7a–7c
wayfinding (rail, Settings pointer, Admin note).

Canvas, controls, inspector placement and keyboard model are shared with Stack
(`stack-view-design.md`): pan/zoom, Fit / 1:1 / % / LOD note, floating inspector top-right,
the canvas is a keyboard listbox.

## Not in this view

Live counts on stages, observed transitions (PRD §7), per-skill nodes, any control that
switches a rule's enforcement on or off.

## Acceptance (frontend)

1. Renders from the registry route; no hardcoded rules, stages' state or counts.
2. Every level renders with icon + word; legend counts match the registry.
3. A stage is never drawn stronger than its weakest used rule (test with Review: 2 enforced +
   2 advisory → ADVISORY).
4. Editing a gitops field and the budget cap round-trips; gitops → ADVISORY, cap stays
   EXTERNAL; a cap edit leaves every other policy key unchanged.
5. No input renders when `control.writable` is false; unreachable shows no values.
6. "no result recorded", "not connected", "isn't mapped to a rule yet" each render as their
   own state, never blank.
7. Overrides 1–5 above hold in the built view.
8. Keyboard and screen-reader walk passes (PRD-52 bar). Frontend tests + typecheck green.
