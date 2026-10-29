# Rules view — Claude Design prompt set (GRPH-P61)

Source PRD: **GRPH-P61, "Rules view: the project's delivery process as one map"** (approved v1.0,
2026-10-28). The ledger is the source of truth; this file turns it into screens to design
**before** anything is built, so the UX is settled before decomposition.

**Workflow:** paste **Part 1** verbatim at the top, then **Part 2** (the data contract and seed),
then **one** screen prompt from **Part 3**. One `*.dc.html` per screen. Push the results to the
Claude Design project **graphban-local** under the `Build` card group.

**Revising the existing design:** paste Part 1, then Part 2, then **Part 4** in the same thread
as `Rules.dc.html` / `Rules - Hi-fi.dc.html`. Parts 1–3 were corrected on 2026-10-28 after the
design review against the backend (GRPH-P61 rebaseline); Part 4 lists what changed.

---

## Part 1 — Shared preamble (paste at the top of every screen)

> **Product.** Graphban is an agent-native dev tool: a tracker, agent memory, PRDs that must
> survive a "grill" before approval, a code-structure graph, and a fleet of coding agents that
> claim, build, review and merge work through MCP tools. You are designing one new view in the
> project's **Build** section: **Rules**.
>
> **What Rules is.** Today the rules that govern how work moves through a project are scattered:
> a gitops contract (base branch, no-push-to-base, branch/PR naming, reviewer bar, version
> source), server-side gates (a PRD's approval is *earned* by finishing its grill and can never be
> set), the review rules (an agent can never sign off its own work; reviewers must be a different
> vendor), seat and delegation rules, budget caps, scan cadence, and the skills pack agents read.
> Rules draws all of them as **one flow**: stages as nodes, rules attached to the stage they
> govern, each rule saying **what holds it** and **where its value lives**. Its job is to answer
> the question people and agents actually ask: **"why was I refused?"**
>
> **The one product rule this view exists to honour: a drawn rule must not read as an enforced
> rule.** This is Graphban's recurring defect class ("absence reads as clean"). Every rule carries
> one of five enforcement levels, strongest first, and they must look *different at a glance* —
> icon **and** word, never colour alone:
> - **ENFORCED** — the Graphban server refuses. Strongest. Solid. Rare: only rules with a server
>   refusal path.
> - **CHECKED** — CI or a scan reports; nothing refuses. Shows its last result and date.
> - **EXTERNAL** — something outside Graphban is supposed to hold it (the forge's branch
>   protection, the fleet supervisor's budget cap) and Graphban **can't verify** that it does.
>   Always carries "not verified".
> - **ADVISORY** — agents are *told* it and nothing checks: either sent to every agent in its
>   context (the gitops contract) or written in a skill / AGENTS.md. Visibly weaker.
> - **UNMEASURED** — a contract field with no value. Visibly *absent*, never neutral.
>
> **The gitops contract is ADVISORY, not ENFORCED.** Graphban hands the contract to agents; no
> server code refuses a violation. Setting a value moves a rule from UNMEASURED to ADVISORY —
> never to ENFORCED.
>
> A sixth, rule-level state: **NOT USED** — a rule this project's gitops model doesn't use (e.g.
> `push_to_base` has no PR title). It is drawn dimmed and labelled, **never hidden**. A *stage* is
> NOT USED only if every rule on it is.
>
> **A stage looks only as strong as its weakest used rule.** A stage with five enforced rules and
> one advisory rule is drawn as advisory, with "5 enforced · 1 advisory" beneath it. Never let an
> enforced-looking box hide a soft rule.
>
> **Editable vs informational is decided by where the value lives** (`truth`):
> - **db** — a value the server owns (the gitops fields). Editable in place, *if* the viewer may
>   write and the field is not inherited from the org.
> - **code** — hard-coded in a service. Read-only; shows the code path; one line on why.
> - **repo_file** — a skill, AGENTS.md, a TOML matrix. Read-only; shows the file path.
> - **derived** — computed from other state. Read-only.
>
> Gitops fields can be **inherited from the org**. Show the *effective* value and its source
> (`project` · `org` · `unmeasured`). An org-sourced field is not editable here; it links to
> **Admin → Gitops**.
>
> **Visual system (the live app's tokens — match exactly; dark only):**
> - Canvas `#0a0b0d`; surfaces `#0f1317` / `#12171b` / `#1b2127` / `#202831` (rising
>   elevation); header `rgba(12,15,17,.72)` with blur.
> - Hairlines `#2a333a` / `#1e242a` (structure only); **control borders `#626c75`** (anything
>   operable — inputs, toggles — holds ≥3:1).
> - Text: fg `#e6e9ec`, fg-2 `#dfe3e7`, muted `#8b949e`, faint `#7f8891`.
> - Accent **lime** `#c6f24e` (hover `#d8ff74`) — primary actions, active nav, focus ring,
>   selection.
> - Status: green `#5fd07a`, amber `#e0b34a`, blue `#7ca2ff`, red `#ff6b6b`, purple `#a78bfa` /
>   `#c9b8ff` (agent/AI).
> - Radii: controls 8px, cards 12px, overlays 16px. Shadows are layered and neutral.
> - Type: **IBM Plex Sans** for UI, **IBM Plex Mono** for ids, paths, values, rule ids, counts.
>   Micro-labels mono, 10–11px, UPPERCASE, letter-spacing ~.7px, faint.
> - Status pill = 6px dot + mono uppercase label in a tinted-border pill. Thin line icons, 1.8
>   stroke.
> - Tone: precise, engineer-facing, quietly confident. Real data, never lorem ipsum.
>
> **Shell.** Left rail ~200px with collapsible sections PLAN · BUILD · OBSERVE. Rules sits in
> **BUILD**: Links · Code graph · Stack · **Rules** · MCP Tools · Fleet · Outposts. Top bar with
> project switcher (`GRPH` · Graphban). The canvas fills the content pane.
>
> **Sister view: Stack.** Build → Stack is an existing pan/zoom canvas (nested frames, drawn
> labelled edges, a floating ~368px inspector top-right, bottom bar with zoom − / % / + / Fit /
> 1:1 and a level-of-detail note). **Rules must feel like the same family**: same canvas
> controls, same inspector placement, same keyboard model (the canvas is a keyboard listbox —
> arrows move between nodes, Enter opens the inspector, Esc closes it).
>
> **Two rules that are product decisions, not styling — apply them on every screen:**
> 1. **An empty state names which kind of empty it is.** "Nothing configured", "not measured
>    yet", "could not load" and "not used by this model" are different facts and never render
>    the same way.
> 2. **No result is not a pass.** A checked rule with no recorded result says "no result
>    recorded", never blank, never green.
>
> **`.dc.html` conventions:** wrap in `<x-dc>`; fonts + a `<style>` reset in `<helmet>`; use
> `<sc-for list="{{ rows }}" as="r" hint-placeholder-count="N">` for lists and
> `<sc-if value="{{ flag }}">` for conditionals; bind with `{{ }}`; wire events with
> `onClick="{{ handler }}"` and hover with `style-hover="…"`. Drive it from a
> `<script type="text/x-dc" data-dc-script data-props="{&quot;$preview&quot;:{&quot;width&quot;:1440,&quot;height&quot;:900}}">`
> block containing `class Component extends DCLogic { state = {…}; renderVals(){ return {…}; } }`.
> Include the seed data from Part 2 and at least the interactive state the screen hinges on.

---

## Part 2 — Data contract and seed (paste under Part 1)

> **Nothing here exists yet except the gitops fields.** The registry is what GRPH-P61 will
> build. Draw only these fields; **do not invent new ones** (no owners, no severities, no
> per-rule toggles, no "disable rule" buttons — rules describe what code already enforces; this
> view cannot turn enforcement on or off).
>
> **A rule:**
> ```
> id           gitops.no_push_to_base          (stable slug, mono)
> stage        spec | plan | build | review | merge | release | learn
> title        "No direct push to the base branch"
> summary      one plain line
> enforcement  enforced | checked | external | advisory | unmeasured
> channel      (advisory only) context | skill | doc   — how agents are told
> enforcer     { kind, ref, url }  kind: graphban_source | ci_job | scan | skill | doc |
>              context | forge | supervisor
> truth        db | code | repo_file | derived
> scope        project | org | platform
> models       gitops models the rule applies to (default all) → drives NOT USED
> value        (db rules only) the effective value
> source       (db rules only) project | org | unmeasured
> last_result  (checked rules only) { outcome: pass|fail, at: date } | null
> ```
> A `graphban_source` enforcer links to **Graphban's own source at the running release tag**
> (e.g. `github.com/asc-me/graphban/blob/2026.10.23/backend/app/services/prds.py`) — never to
> the project's code graph, which is the tenant's repo.
>
> **A stage:** `id`, `label`, its rules, a derived `weakest` (over used rules) and `used` (false
> only when every rule is not used).
>
> **Gitops control state** (exists today): `local` (this box owns it, editable) ·
> `linked_set` (org owns it, value present) · `linked_unset` (org owns it, empty) ·
> `linked_unreachable` (org owns it, could not be reached — **the server returns no values at
> all**; every gitops rule is UNMEASURED; there is no last-known copy).
>
> **Refusal link** (new): a refusal response may carry `rule_id`. If it does not, the UI says
> "this refusal isn't mapped to a rule yet". Unmapped refusals are recorded as
> `refusal_unmapped` events `{route_template | mcp_tool, status, count, last_seen}` — a route
> template (`PATCH /api/items/{id}`), never a concrete path. They carry **no stage**.
> **Coverage** comes from the build: `{named, total}` = refusal sites that name a rule, out of
> all typed refusal sites.
>
> **Seed — project GRPH (Graphban), gitops model `prs_to_base`:**
>
> | stage | rule id | title | enforcement | truth | enforcer / value | not used under |
> |---|---|---|---|---|---|---|
> | spec | `gate.approval_earned_by_grill` | Approval is earned by finishing the grill, never set | enforced | code | `graphban_source` `services/prds.py` | — |
> | spec | `gate.grill_dimensions_closed` | All four grill dimensions closed before approval | enforced | code | `graphban_source` `services/prds.py` | — |
> | plan | `plan.planner_only_minting` | Only a planner can mint enrolment seats | enforced | code | `graphban_source` `services/fleet.py` | — |
> | plan | `plan.budget_cap` | Item token budget cap | **external** | db | `supervisor` gbfleet · cap 400k tokens (source: project) · "not verified" | — |
> | build | `gitops.branch_name_pattern` | Branch name pattern | **advisory** (context) | db | `{tag}-{item_id}-{slug}` (source: org) | — |
> | build | `build.reach_refusal` | Item text may not instruct an out-of-reach act | enforced | code | `graphban_source` `services/reach.py` | — |
> | build | `build.heartbeat` | Claims lapse without a heartbeat (150 s) | enforced | code | `graphban_source` `services/fleet.py` | — |
> | review | `review.not_own_work` | An agent can never sign off its own work | enforced | code | `graphban_source` `services/fleet.py · SelfReview` | — |
> | review | `review.reviewer_diversity` | Reviewer must be a different vendor | enforced | code | `graphban_source` `services/fleet.py` | — |
> | review | `gitops.reviewer_bar` | Reviewer bar | **advisory** (context) | db | `Graphban sign_off` (source: project) | push_to_base, when value is `forge` |
> | review | `review.sabotage_before_sign_off` | Reviewer runs a sabotage before signing | advisory (skill) | repo_file | `skill` `graphban-review/SKILL.md` | — |
> | merge | `gitops.base_branch` | Base branch | **advisory** (context) | db | `main` (source: org) | — (still used under push_to_base) |
> | merge | `gitops.no_push_to_base` | No direct push to base | **advisory** (context) | db | `true` (source: org) | push_to_base |
> | merge | `gitops.pr_title_pattern` | PR title pattern | unmeasured | db | — | push_to_base |
> | merge | `merge.ci_green` | Merged work passed CI | checked | derived | `ci_job` `CI` · last `suite_green` on a merged item: pass · 2h ago | — |
> | merge | `merge.ci_required` | `CI` is the sole required check | **external** | derived | `forge` GitHub branch protection · "not verified" | — |
> | release | `gitops.version_from` | Version source | **advisory** (context) | db | `CalVer tag` (source: project) | — |
> | release | `gitops.release_defined_in` | Release definition | unmeasured | db | — (placeholder: "not docs/release.md") | — |
> | learn | `scan.security_cadence` | Security scan per release | checked | derived | `scan` kind `security` · **no result recorded** | — |
> | learn | `learn.lessons_reviewed` | Mined lessons are reviewed before publishing | enforced | code | `graphban_source` `services/memory.py` | — |
>
> Legend totals for this seed: **ENFORCED 8 · CHECKED 2 · EXTERNAL 2 · ADVISORY 6 · UNMEASURED 2**.
>
> Plus one collapsed node, off the main flow, linked to Build/Review: **Agent instructions
> (Graphban pack)** — advisory · repo_file · `9 skills shipped with gban 0.8.2` · links to
> the skills.
>
> **Unmapped refusals seed:** `PATCH /api/items/{id}` 422 × 41 (last 3 min ago) ·
> MCP `claim_next` 409 × 17 · `POST /api/projects/{id}/links` 400 × 3 ·
> `PUT /api/fleet/agents/{id}/role` 403 × 1. Coverage: **11 of 46** refusal sites name their rule.

---

## Part 3 — Screen prompts

### 1 · Rules map — default  *(P0, the flagship)*
> **Screen: Rules map.** Build → Rules for project GRPH. The canvas shows the seven stages left to
> right — **Spec → Plan → Build → Review → Merge → Release → Learn** — joined by fixed directional
> edges (this is the canonical process, not observed traffic). Each stage is a card: stage name,
> a weakest-enforcement pill, and a mono tally ("4 enforced · 1 advisory"). Under the card, up to
> three rule chips are visible at 100% zoom; the rest fold into "+3 more". At low zoom only the
> card, its name and its pill remain.
> Review is drawn **ADVISORY** despite two enforced rules, because of the sabotage rule and the
> reviewer bar. Plan is drawn **EXTERNAL** because the budget cap is held by the fleet
> supervisor, not the server. Merge and Release are drawn **UNMEASURED** because of their empty
> gitops fields. Make the reader *see* that a strong-looking stage is soft.
> The **Agent instructions (Graphban pack)** box sits below the flow, collapsed, dashed border,
> advisory, with thin connectors to Build and Review.
> A **legend** is always visible (bottom-left, docked): ENFORCED · CHECKED · EXTERNAL ·
> ADVISORY · UNMEASURED · NOT USED, each icon + word.
> Header: "Rules" title, a one-line subtitle ("How work moves through GRPH, and what holds each
> step"), the gitops model chip `prs_to_base`, and a right-aligned **Unmapped refusals · 62**
> link (amber) that opens screen 5.
> Bottom bar matches Stack: zoom − / % / + / Fit / 1:1, and the level-of-detail note.
> **States:** default (Fit), hover on a stage (outlines its rules and its edges), keyboard focus
> on Review (lime focus ring, visible without a mouse).

### 2 · Stage inspector — Review  *(P0)*
> **Screen: Stage inspector.** Same canvas, Review selected. The floating inspector (~380px,
> top-right, like Stack) shows: stage name + weakest pill + tally; a one-line summary of the
> stage; then **rule rows**. Each row has: enforcement icon + word (left, fixed width), title,
> summary, a mono `rule id`, a **truth** chip (`code` / `db` / `repo_file` / `derived`), and the
> enforcer as a mono link with an external-link glyph. Order rows weakest first: advisory before
> enforced, so the soft rule is the first thing read.
> The `code` rows end with a quiet line: "Held in code. Change it with a PR, not here."
> The `repo_file` row (sabotage) reads: "Written in the review skill. Nothing checks it."
> Also show a variant with **Merge** selected, to draw a **CHECKED** rule: `Merged work passed
> CI` with a green "PASS · 2h ago" result, next to the **EXTERNAL** rule `CI is the sole required
> check` ("GitHub branch protection · not verified"), and on **Learn** the security scan row
> reading "**no result recorded**" in a neutral, unmistakably-not-green treatment.
> **States:** Review selected; Merge selected; Learn selected; the inspector's empty state when
> a stage has zero registered rules ("no rules registered for this stage yet" — distinct from
> "none apply").

### 3 · Editing a gitops rule — Merge  *(P0)*
> **Screen: Editing in place.** Merge selected. The `db` rows are editable, using the existing
> gitops field editors (text input with token chips for patterns; a toggle for no-push; a
> select for the reviewer bar). Draw all four control states side by side as stacked variants:
> (a) **local** — `pr_title_pattern` is UNMEASURED and editable; the input's placeholder is
> "Unmeasured — not set"; the user types `{tag}-{item_id}: {slug}` and token chips
> (`item_id`, `tag`, `slug`, `version`, `date`) insert at the cursor. Save / Cancel appear only
> once dirty. After saving, the row's pill changes from UNMEASURED to **ADVISORY** (never
> ENFORCED) with "Told to agents in every get_context. Nothing refuses a violation.", and the
> Merge stage's weakest pill updates on the canvas behind.
> (b) **linked_set** — `base_branch = main`, source **org**: read-only value, an "Inherited from
> Ascme Labs" line, and a link "Edit in Admin → Gitops".
> (c) **linked_unset** — inherited from org but empty: UNMEASURED, read-only, "Your org has not
> set this" + the Admin link.
> (d) **linked_unreachable** — **no values at all**: every gitops row UNMEASURED under an "org
> unreachable" banner; nothing editable. There is no last-known copy to show.
> Also: a **viewer without write access** sees (a) as read-only with "Only an org owner or
> admin can change this" (org project) or "You don't have write access to this project".
> **States:** clean, dirty, saving, save failed (inline, the server's message verbatim, the typed
> value kept), saved.

### 4 · Following a refusal  *(P0)*
> **Screen: "Why was I refused?"** Two places a refusal surfaces, and where each lands.
> (a) **In an item's activity feed** (draw a compact slice of the item detail panel): an agent
> event "`GRPH-A4117` tried to set this PRD to approved — refused (409)" with a **see rule**
> link. Clicking it lands on the Rules map with **Spec** selected, the
> `gate.approval_earned_by_grill` row highlighted (lime left bar, scrolled into view), and a
> small breadcrumb at the top of the inspector: "From: refusal on GRPH-P61 · 409".
> (b) **An unmapped refusal**: an event "`PATCH /api/items/GRPH-1102` refused (422)" with the
> quiet line "this refusal isn't mapped to a rule yet" and a link "see all rules", which opens the
> map unfocused with the **Unmapped refusals** link pulsing once.
> Make (a) and (b) clearly different: (b) must not look like a broken link or like "no rule
> applies".

### 5 · Unmapped refusals  *(P1)*
> **Screen: Unmapped refusals.** A side sheet (or the inspector in a second mode) opened from the
> header link. A ranked table: route (mono), status, count, last seen, and a sparkline of the
> last 7 days. Header copy: "Refusals the server made without naming a rule. Each one is a gap
> in this map." Above the table, a progress line: "11 of 46 refusal sites name their rule" with
> a thin bar — it can grow but never shrink (CI ratchet).
> No "create rule" action — mapping a refusal is a code change. Each row offers "copy route" and
> "file as item".
> **States:** populated (seed), **none recorded yet** ("no unmapped refusals recorded since
> <date>" — say since when, so zero reads as a measurement, not a blank), could not load.

### 6 · Edge states  *(P1)*
> **Screen: Edge states, stacked as variants on one card.**
> (a) **`push_to_base` model** — only the rules the model drops are not used: `pr_title_pattern`
> and `no_push_to_base` (and `reviewer_bar` if it is `forge`). Those rows render under a **NOT
> USED** treatment (dimmed, dashed, labelled "not used by this project's model: push_to_base").
> `base_branch` and the CI rules stay live, so Merge is still drawn at its weakest *used* rule,
> not NOT USED.
> (b) **Brand-new project, nothing measured** — every gitops rule UNMEASURED; the code rules are
> still ENFORCED (they hold regardless). A banner: "Your delivery contract is unmeasured. The
> server's own gates still apply." with a primary action to the Merge stage.
> (c) **Loading** — stage skeletons in place, no legend counts.
> (d) **Could not load** — an error card that cannot be mistaken for (b): red, the error text,
> retry. Never draw an empty map here.
> (e) **Narrow viewport (≤ 900px)** — the canvas becomes a vertical stage list; the inspector
> becomes a bottom sheet. Same pills, same order.

### 7 · Nav and the old Gitops entry  *(P2)*
> **Screen: Wayfinding.** (a) The left rail with **Rules** in BUILD (icon: a branching flow,
> line style), active. (b) Project **Settings → Gitops** now shows a one-card pointer: "Gitops
> moved into Rules. Your delivery contract is the Merge and Release stages." with a button to
> Rules focused on Merge. (c) **Admin → Gitops** (org level) unchanged in structure, with one
> added line: "Projects see these values on their Rules map as inherited."

---

## Notes for whoever runs the set

- **Design 1 → 2 → 3 first.** The map, the inspector and in-place editing are the whole argument.
  If those three feel right, 4–7 are variations.
- **The weakest-rule treatment is the screen to spend time on.** A process diagram that makes
  soft rules look hard is worse than the gitops form it replaces. If a reviewer glancing at the
  map can't tell Review is soft, the design has failed regardless of how good it looks.
- **Reuse Stack's canvas, controls and inspector by name.** The two views should be obviously
  siblings.
- **Open questions the designs may help settle** (still open in GRPH-P61 §9):
  the view's name ("Rules" vs "Process") — label one variant each way on screen 1 if useful;
  how a stale CHECKED result reads ("checked 14 days ago" alone, or flagged past a threshold);
  how long unmapped refusals are kept.
- **Out of scope — do not draw:** live counts on stages ("holding 3 items"), observed
  transitions, per-skill nodes, any control that turns a rule on or off.

---

## Part 4 — Revision 1 to `Rules.dc.html` (paste under Parts 1–2)

> **Revise `Rules.dc.html` and `Rules - Hi-fi.dc.html`. Keep everything not listed here** — the
> canvas, weakest-first sorting, the striped tally bars, the hatched "no result recorded" box,
> the keyboard listbox, the distinct empty/loading/error states, and the refusal frames are
> right. A review of the design against the backend found that the seed I gave you was wrong
> about what the server enforces, so the map was drawing the exact defect it exists to prevent.
> Seven changes, then re-export every Hi-fi frame.
>
> **1 · Five enforcement levels, not four.** Add **EXTERNAL** between CHECKED and ADVISORY:
> something outside Graphban is supposed to hold the rule and Graphban can't verify it. It needs
> its own icon, border and tally-bar treatment, distinguishable from both CHECKED (blue, solid)
> and ADVISORY (amber, dashed) without colour — e.g. a solid border with an open/unknown glyph,
> and the words "not verified" on every row. Add it to the legend between CHECKED and ADVISORY.
> Rank it below CHECKED for the weakest-stage calculation.
>
> **2 · Re-seed from Part 2.** Every gitops rule is **ADVISORY** (channel: context), not
> ENFORCED. The budget cap is **EXTERNAL** (fleet supervisor). The old `merge.ci_required` splits
> into `merge.ci_green` (CHECKED, PASS · 2h ago) and `merge.ci_required` (EXTERNAL, GitHub
> branch protection). The legend must read **ENFORCED 8 · CHECKED 2 · EXTERNAL 2 · ADVISORY 6 ·
> UNMEASURED 2**. The canvas changes: Plan → EXTERNAL; Build → ADVISORY (branch pattern); Review
> → ADVISORY (two soft rules now); Merge and Release stay UNMEASURED. Advisory gitops rows
> carry the quiet line "Told to agents in every get_context. Nothing refuses a violation." —
> distinct from the skill row's "Written in the review skill. Nothing checks it."
>
> **3 · Saving never makes a rule ENFORCED (frames 3a5, 3f).** After saving `pr_title_pattern`
> the pill goes UNMEASURED → **ADVISORY**. Merge's card pill then reads ADVISORY (it was
> UNMEASURED, and before this revision wrongly CHECKED). Use `{tag}-{item_id}: {slug}`.
>
> **4 · `push_to_base` is per rule, not per stage (frame 6a).** Do not grey out the Merge stage.
> Only `pr_title_pattern` and `no_push_to_base` (which the model contradicts) read NOT USED.
> `base_branch`, `ci_green` and `ci_required` stay live, so Merge is drawn at its weakest used
> rule. Its tally reads e.g. "1 checked · 1 external · 1 advisory · 2 not used". A stage reads
> NOT USED only when every rule on it is not used — show that nowhere in the seed, since no
> stage qualifies.
>
> **5 · Unreachable org shows nothing (frame 3d and the scenario).** Remove the faded "last
> known" values and "ORG · ASCME LABS · LAST KNOWN" source. The server returns no values when the
> org is unreachable. Every gitops row is UNMEASURED under the amber "org unreachable" banner;
> nothing is editable. Banner copy: "Couldn't reach Ascme Labs' gitops. The contract is
> unmeasured until it's back — Graphban never falls back to an old copy."
>
> **6 · Keep the Recommended rules panel (frame 5d), with three corrections.** It is in v1.
> (a) A suggestion never changes enforcement: a value suggestion prefills the editor, and saving
> it yields **ADVISORY**. Replace "WOULD BE ENFORCED" chips and "Setting it lets the server hold
> it" with what actually happens ("Agents would be told this in every get_context. Nothing would
> refuse a violation."). A code suggestion keeps "File as item". (b) The panel's header lists
> every evidence source and its state: `Stack · read`, `Code graph · read`, `GitHub · read` — and
> add a variant **5e** where GitHub is **not connected**: that source reads "not connected ·
> Connect GitHub", and the PR-title suggestion is absent with a line saying why. (c) The
> security-scan suggestion's evidence cites `scan.security_cadence · no result recorded` — keep
> that, it's the right tone.
>
> **7 · Linked and read-only rows have no inputs.** Wherever `control.writable` is false (any
> linked state, unreachable, or a viewer without write access), the gitops rows are read-only
> text — no greyed-out inputs. Only `local` + writable renders the editors.
>
> **Smaller corrections:**
> - **Enforcer links.** `graphban_source` rows link to Graphban's source at the running tag
>   (`github.com/asc-me/graphban/blob/2026.10.23/backend/app/services/prds.py`), shown as
>   `graphban · services/prds.py @ 2026.10.23` — not to the project's Code graph. CI rows link to
>   the CI job, scan rows to the scan, skill rows to the skill, forge rows to the repo's
>   branch-protection settings, supervisor rows to Build → Fleet. Show the `kind` as a small mono
>   prefix on the enforcer.
> - **Frame 2d copy.** Unmapped refusals carry no stage. Replace "refusals in this stage land in
>   Unmapped refusals" with "Nothing in the registry governs this stage yet. That isn't the same
>   as nothing applying."
> - **Frame 5a.** Group the MCP row by tool: `MCP · claim_next`, not `POST /api/mcp claim_next`.
>   Coverage line: "11 of 46 refusal sites name their rule".
> - **Read-only copy (3e).** "Only an org owner or admin can change this" — the server checks org
>   role, not a project-admin role.
> - **Skills node.** "9 skills shipped with gban 0.8.2" — the server lists the pack Graphban
>   ships, not the project's `.claude/skills`.
> - **Left rail.** It renders blank in the `Rules.dc.html` frames (only 7a draws it). Draw it in
>   every full-page frame.
>
> **Re-export** all Hi-fi frames, and add one new frame **2e · Plan selected** showing the
> EXTERNAL budget-cap row: "Held by the gbfleet supervisor on the agent's machine. Graphban can't
> verify it — some vendor rows can't honour a cap at all." with a link to Build → Fleet.
