# GRPH-P67 design set DS-a0e16dd41a — first-pass findings

Reviewed 2026-11-06 from `~/Downloads/graphban - local.zip` (Quick Start, Tour Stop, Setup Status).
`Org Link.dc.html` in the same zip belongs to the org-window work, not this set.

**Not filed yet.** `file_finding` refuses the actor that compiled the prompts, so another
writer (web UI, or a non-compiler key) files these. Every kind except `spec_gap` needs a
`canvas_hash`: store the screen's canvas with `put_canvas` first. A canvas binds only to an
accepted prompt hash, so accept the prompts before uploading.

| # | screen | kind | text |
|---|--------|------|------|
| 1 | Connect your agent | design_edit | Step ③ turns ✓ DONE when the First job screen opens (`connect: … \|\| screen === "first_job"`). Connect goes ✓ only on a recorded first call (`last_used_at`) or a clean machine report; moving on leaves it PENDING. |
| 2 | Org plug | design_edit | "Link this project to an org" → "Link this deployment to an org". Linking is per deployment (`graphban link`, the Org Link window). Also drop "(Gitops)" from "shared house rules". |
| 3 | Tour stop | design_edit | The Fleet stop is drawn SKIPPED ("rail without Fleet.v2") while the rail renders Fleet.v2. Skip a stop only when its anchor is absent; with this nav, stop 7 shows. |
| 4 | Setup status card | design_edit | Settings → Machines shows `ubuntu-srv` with a green REPORTED dot beside "1 FAIL", while the card shows ✗ red. A report with failures uses the failure colour in both places. The terminal inset (`ubuntu-srv` cannot reach `http://ubuntu-srv:8080`) contradicts its 13:40 report; drop it or move it to a machine that has never reported. |
| 5 | Where is it running | spec_gap | 4. Part one — the quick-start dialog: the step-⓪ "What changes" table (this computer vs dedicated server: commands, safety check, Ollama URL, machines, code-graph pass) did not reach the compiled prompt; the canvas drew a stand-in labelled "PRD §4 TABLE NOT ATTACHED". |
| 6 | Tour stop | design_edit | Remove the "Copy on stops other than Tracker is a draft" banner from the tour strip once the stop copy is final. |

## Revision prompts

Paste each into the Claude Design file it names (project graphban-local). Each one changes only
what it lists, so the rest of the first pass stays as it is.

### R1 — Quick Start.dc.html · Connect your agent (finding 1)

> Revise Quick Start.dc.html. Change only the step-③ state logic and what it renders.
>
> Step ③ "Connect your agent" goes ✓ DONE in exactly two cases: the key has a recorded first
> call (paste route: `pasteState === "connected"`), or a machine has a clean `gban setup` /
> `gban doctor` report (CLI route). Nothing else ticks it. Opening the First job screen,
> clicking Next and reaching Finish all leave it ○ PENDING.
>
> Remove `|| screen === "first_job"` from the `connect` done rule. In every preview state
> where the agent has not called in, the stepper shows ③ ○ PENDING on First job and on Finish.
> The Finish line stays "Agent connection unverified — <route>" with "This stays on your
> dashboard until it's proven."
>
> Add one preview state, `connectProof: "none" | "first_call" | "machine_report"` (default
> "none"). With `first_call` or `machine_report`, ③ shows ✓ DONE and the Finish line reads
> "Agent connected — first call 13:42 from alex-mbp" or "Agent connected — alex-mbp
> reported clean 13:42".

### R2 — Tour Stop.dc.html · Org plug (finding 2)

> Revise Tour Stop.dc.html. Change only the Org plug stop's copy.
>
> Linking is per deployment, not per project. Replace "Link this project to an org" with
> "Link this deployment to an org" in the popover body and the strip card. The body becomes:
> "Working with a team? Link this deployment to an org and you get shared house rules,
> memory that carries across projects, one roster of everyone's agents, and capacity routing
> across your team's entitlements. Your code graph stays local-first."
>
> Drop "(Gitops)". Keep "Not now" as an equal choice in every org variant. The request form
> and its states don't change.

### R3 — Tour Stop.dc.html · Fleet stop and the draft banner (findings 3 and 6)

> Revise Tour Stop.dc.html. Two changes; nothing else moves.
>
> 1. A stop is SKIPPED only when its anchor is missing from the rendered rail. The rail in
>    this file renders Fleet.v2, so stop 7 "Fleet" anchors on rail › Fleet.v2 and plays like
>    the others. Its strip card loses the dashed border and the "SKIPPED · RAIL WITHOUT
>    FLEET.V2" note. Add a preview prop `fleetInNav: boolean` (default true). Only when it is
>    false is stop 7 skipped: dashed card, note "SKIPPED · no Fleet entry in this nav", and
>    the counter shows "/ 7".
> 2. Remove the strip header text "Copy on stops other than Tracker is a draft." Keep
>    "TOUR · 8 STOPS + ORG PLUG".

### R4 — Setup Status.dc.html · state colours and the inset (finding 4)

> Revise Setup Status.dc.html. Change only the colours of the Machines STATE column and the
> terminal inset.
>
> One machine has one state everywhere. `ubuntu-srv` reported with one failure, so in
> Settings → Machines its STATE reads "× REPORTED · 1 FAIL", all in the failure red the
> dashboard card uses (#ff6b6b family). No green dot. Green is reserved for "✓ CLEAN" (a
> report with zero failures). "NEVER REPORTED" stays grey with ○.
>
> Move the terminal inset to `build-box`, which has never reported: header "INSET ·
> TERMINAL ON build-box", body `$ gban setup` … `UNKNOWN  report  could not report to
> http://ubuntu-srv:8080: connection refused`. Add one line under it: "The card keeps
> build-box at ○ never reported. A failed report is not a report." Remove the old inset,
> which showed ubuntu-srv unable to reach its own address.
>
> Add a third Machines row state for the populated preview: `alex-mbp`, macOS 15.1, gban
> 2026.11.1, gbfleet 2026.11.1, harnesses "claude, grok", last report 5 Nov 13:42, "✓ CLEAN".
> With that state on, the dashboard card's header becomes "✓ Agent connection verified —
> alex-mbp reported clean".

### R5 — Quick Start.dc.html · Where table (finding 5)

Text from GRPH-P67 §4 step ⓪ (`docs/design/guided-setup-plan.md`, "What changes with the answer").

> Revise Quick Start.dc.html. Change only step ⓪. Replace the Advanced table labelled
> "STAND-IN — PRD §4 TABLE NOT ATTACHED" with the table below, and remove the stand-in chip.
>
> | | On this computer | Dedicated server |
> | --- | --- | --- |
> | Commands in ③ | use `localhost` | use the entered server address, with "run this curl from your dev machine to check" |
> | Safety check (①) | quiet: loopback-only boxes aren't exposed; any warnings collapse into one line | **active**: `JWT_SECRET`, `SIGNUP_MODE=invite_only`, `SECRET_ENCRYPTION_KEY`, `REQUIRE_STRONG_SECRET` become main-path warnings, and `TRUSTED_HOPS`/HTTPS go under Advanced |
> | Ollama URL (②) | `http://host.docker.internal:11434` under compose, `localhost` natively | asks where Ollama runs (on the server, or elsewhere on the network) |
> | Machines (③) | one machine, this one; the "more machines" hint stays in Advanced | multi-machine by design: the "run this on each dev machine" copy moves to the main path |
> | Code-graph pass (④) | runs locally, no notes | the agent runs on the dev machine and talks to the server over the network, and the prompt says so |
>
> When "On a dedicated server" is selected, show a **Server address** field under it,
> prefilled from the browser origin (`http://ubuntu-srv:8080`), with "✓ reachable from this
> browser" beside it. Every later step's commands use this value.
>
> The pre-selection note names its evidence. With `origin: localhost`, it reads
> "Pre-selected: you opened Graphban on localhost." With `origin: ubuntu-srv`, it reads
> "Pre-selected: you opened Graphban at ubuntu-srv:8080." The person can always change it.
> Hosted invitees never see step ⓪.

## Second pass (zip of 2026-11-06 12:12)

| Prompt | Result |
|---|---|
| R1 Connect proof | **Fixed.** ③ stays PENDING on First job and Finish with no proof; `connectProof` turns it ✓ with the evidence line. |
| R2 Org plug copy | **Fixed.** "this deployment"; "(Gitops)" gone. |
| R3 Fleet stop + banner | **Fixed.** Fleet plays by default; `fleetInNav: false` skips it and counts "/ 7". Banner removed. Two leftovers below (R3b). |
| R4 Setup Status | **Fixed.** "× REPORTED · 1 FAIL" in red, "✓ CLEAN", "○ NEVER REPORTED"; inset moved to build-box; verified header. One inconsistency below (R4b; my prompt caused it). |
| R5 Where table | **Not applied.** The canvas now shows a "—" table captioned "Cells unknown — GRPH-P67 §4 step ⓪ text not received." The server-address field, "✓ reachable from this browser" and the evidence-naming pre-selection note did land. Resend as R5b. |

### R5b — Quick Start.dc.html · Where table, resend

> Revise Quick Start.dc.html, step ⓪ Advanced only. The cell text for the "What changes"
> table is below. It is the GRPH-P67 §4 step ⓪ text, copied verbatim, so use it as given.
> Replace every "—" cell and remove the caption "Cells unknown — GRPH-P67 §4 step ⓪ text not
> received."
>
> - **Commands in ③.** This computer: use `localhost`. Dedicated server: use the entered
>   server address, with "run this curl from your dev machine to check".
> - **Safety check (①).** This computer: quiet; loopback-only boxes aren't exposed, and any
>   warnings collapse into one line. Dedicated server: active; `JWT_SECRET`,
>   `SIGNUP_MODE=invite_only`, `SECRET_ENCRYPTION_KEY` and `REQUIRE_STRONG_SECRET` become
>   main-path warnings, and `TRUSTED_HOPS`/HTTPS go under Advanced.
> - **Ollama URL (②).** This computer: `http://host.docker.internal:11434` under compose,
>   `localhost` natively. Dedicated server: asks where Ollama runs (on the server, or
>   elsewhere on the network).
> - **Machines (③).** This computer: one machine, this one; the "more machines" hint stays
>   in Advanced. Dedicated server: multi-machine by design; the "run this on each dev
>   machine" copy moves to the main path.
> - **Code-graph pass (④).** This computer: runs locally, no notes. Dedicated server: the
>   agent runs on the dev machine and talks to the server over the network, and the prompt
>   says so.

### R3b — Tour Stop.dc.html · leftovers from the Fleet skip

> Revise Tour Stop.dc.html. Only for `fleetInNav: false`:
> 1. The strip header reads "TOUR · 7 STOPS + ORG PLUG". It shows "8 STOPS" only when Fleet is
>    in the nav.
> 2. The spotlight ring on the Activity stop wraps only the Activity rail row. Right now it
>    also takes in the "OBSERVE" group label above it, because the row moved up when Fleet.v2
>    was removed. Re-measure the anchor after the nav filters.

### R4b — Setup Status.dc.html · one alex-mbp

> Revise Setup Status.dc.html. In `alexState: clean`, the alex-mbp row reads macOS 26.1,
> gban 2026.11.2, gbfleet 2026.11.2 and harnesses "Claude Code, Grok", the same values as the
> other alex-mbp states. Only LAST REPORT (5 Nov 13:42) and STATE (✓ CLEAN) differ.

## Third pass (zip of 2026-11-06 14:58)

| Prompt | Result |
|---|---|
| R5b Where table | **Not applied.** `Quick Start.dc.html` is byte-identical to the second pass; the "—" table and "text not received" caption are still there. |
| R3b Tour leftovers | **Header fixed** ("TOUR · 7 STOPS + ORG PLUG" without Fleet, "8 STOPS" with it). **Ring still off on first load** without Fleet: it wraps "OBSERVE" + Activity. The new re-measure runs only on `goTo`; the mount path still measures once at 60 ms, before layout settles. A later `resize` puts the ring on Activity, so the anchor is right and the timing is wrong. See R3c. |
| R4b one alex-mbp | **Fixed.** macOS 26.1, 2026.11.2 / 2026.11.2, "Claude Code, Grok", ✓ CLEAN. |

### R3c — Tour Stop.dc.html · ring on first load

> Revise Tour Stop.dc.html, spotlight measurement only. In `componentDidMount`, call
> `this.remeasure()` instead of the single 60 ms `measure()`, and measure once more after
> `document.fonts.ready` resolves. With `stop: 7` and `fleetInNav: false`, the ring must sit
> on the Activity row the moment the frame opens, with the "OBSERVE" label outside it. Also
> cut the ring's vertical padding from 8px to 4px so it does not cover half of the rows above
> and below; keep the 8px horizontal padding.

## Fourth pass (zip of 2026-11-06 16:39)

| Prompt | Result |
|---|---|
| R5 / R5b Where table | **Fixed, via the recompiled prompt** (a revision paste never got it in). All five rows match the plan's step ⓪ table; the "text not received" caption is gone. Nit: env var names render as prose, not `code`. |
| R3c Ring on first load | **Fixed.** Re-measures on mount and after `document.fonts.ready`; vertical padding 4px. With Fleet out the ring sits on Activity; with Fleet in it sits on Fleet.v2 without covering Links / Fleet.v1. |

Every finding (1–6) is now resolved in the canvas. What remains is filing, not design.

## Not for this set

- **Link key format.** This set's invitee Finish screen uses `gb_sk_…`, matching what
  `graphban link --api-key` takes today (`backend/app/cli.py`). The Org Link design uses
  `glk_…`. One of them changes; the org-window owner decides.
