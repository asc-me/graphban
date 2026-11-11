# Graphban concepts — screen prompts (GRPH-P67, Quick Start section 2)

Replaces the concept slides in `Quick Start v2.dc.html` (graphban-local), and specifies the
public concepts page at **graphban.dev/concepts** (Part C).

The first slides had the right subjects but wrong facts: a `Ready` state, a person signing
off as the last step, and items reopening when a spec changes. The board and the five rule
levels were right: the Tracker's default layout is a board (GRPH-1144), and the rules
registry draws exactly those levels (GRPH-P61, `services/rules.py`).

**Scope: the local deployment.** These concepts explain what happens inside one box: items,
claims, review, specs, memory, the code graph, and the rules that hold them. The cloud
console doesn't get this section (see the end).

**Workflow:** paste P67 **Part 1** and **Part 2** from `prd-67-guided-setup-prompts.md`, then
**Part A**, then **one** concept from Part B. One concept per run, all in the same
`Quick Start v2.dc.html` concepts frame. **Part C** is the public page, a separate prompt
with its own file. **Part D** is the full list of concepts to teach and where each one goes.

**Sources**, all read on `origin/main` (2026-11-11): `docs/tracker.md`,
`docs/memory-and-chat.md`, `docs/rules.md`, `docs/mcp.md`, `docs/design-philosophy.md`,
`docs/delegation.md`, PRDs GRPH-P61 and GRPH-P63, and the `graphban-work` / `-review` /
`-prove` / `-prd` / `-scan` / `-orient` skills. If a sentence isn't backed by those, leave it
out.

---

## Part A — Concepts frame (paste under Part 2)

> **Screen: Graphban concepts.** Quick Start section 2. Same dialog, same left hero panel.
> Rail: `01 The loop` · `02 The tracker` · `03 Specs` · `04 Memory` · `05 Code graph` ·
> `06 Rules` · `Go deeper`. Kicker, a title, a lede of at most two sentences, one
> illustration, then at most three rule lines (each starts with an em dash). Footer: Finish
> later · ← Back · Next → (on Go deeper: **Finish concepts**, which returns to Sections and
> marks it ✓).
>
> **Rail and Back.** The kicker counts the six concepts (`CONCEPT n OF 6`) and reads
> `GO DEEPER` on the last panel. Back on concept 01 reads **← Sections**.
>
> **Illustrations are examples, not data.** Put a mono faint `EXAMPLE` micro-label on every
> illustration. Use only the P67 seed: project `Core Platform`, tag `CORE`. Item keys are
> `CORE-n`, PRD keys `CORE-Pn`. No `GRPH-` keys, no invented PRD titles beyond the ones
> below, and no counts that look like the person's own project.
>
> **Words.** The six states are exactly `backlog`, `next`, `in_progress`, `review`,
> `blocked`, `done`, with the tracker colours (`#8b949e`, `#7ca2ff`, `#c6f24e`, `#e0b34a`,
> `#ff6b6b`, `#5fd07a`). There is no `Ready` and no `Kanban`. The tracker has two layouts,
> **Board** (the default) and **List**. "Spec" in prose, and the key prefix stays `-P`.
>
> **Status pills inside an illustration** use the colour of the fact they show, never a
> warning colour for something that is only waiting. A memory `CANDIDATE` is muted
> (`#8b949e`, hairline `#2a333a`), like `pending`. `PUBLISHED` is green. Amber is for tracker
> `review` and scan `has findings`, and red is for `blocked`.
>
> **Do not draw:** an overall health score, a progress percentage, a "mark done" button, or
> a person approving a diff.
>
> **Hero captions:** `CONCEPTS · n/6 · <TITLE IN CAPS>`.

---

## Part B — The concepts (paste one)

### 01 · The loop

> Title "The loop." Lede: "You say what done means. Agents build and review it, and nobody
> signs off their own work."
>
> Illustration: one horizontal row of four nodes, each with a mono role chip:
> `YOU` Spec → `AGENT` Build → `AGENT` Review (a different agent) → `GRAPHBAN` Done.
> Under Review, a muted fork labelled `bounce` loops back to Build. A separate, dashed node
> under the row: `YOU` "Close as merged", for work you merged by hand.
>
> Rules:
> - The builder moves an item to `review`. It never sets `done`.
> - Signing off needs evidence and the commit that was reviewed. A bounce says what failed.
> - Working alone? Review waits for you, or for a second agent.

### 02 · The tracker

> Title "The tracker." Lede: "Every piece of work is an item in one of six states, shown as a
> board or a list. Agents claim the next ready item, and a claim holds its files."
>
> Illustration: the **Board** layout, six narrow columns in state order (backlog, next,
> in_progress, review, blocked, done), each headed by its status dot and name, with one or
> two compact items:
> backlog `CORE-15` · next `CORE-14` "Paginate the export list" · in_progress `CORE-12`
> "Redact tokens in exports" with a mono claim chip `agent-2 · lease 4m` and touchpoints
> `api/export.py`, `api/redact.py` · review `CORE-9` "Add export to the CLI" · blocked
> `CORE-7` "Choose the export file format", with a red line "Waiting on a decision" ·
> done `CORE-5`. A small `Board · List` toggle above the columns, Board selected.
>
> Rules:
> - A cluster claim reserves the files it touches, so two agents never edit the same ones.
> - Agents heartbeat while they work. A lapsed lease frees the item for the next agent.
> - Stopping early releases the item. A blocker that's still true tomorrow is `blocked`.

### 03 · Specs

> Title "Specs." Lede: "Work that needs a decision starts as a spec. The grill asks questions
> until it's testable, and only then does it split into items."
>
> Illustration: a vertical flow:
> `CORE-P3` "Export traces" · `DRAFT` → `/graphban-grill` (mono) "questions until every
> section is testable" → `APPROVED` → three items `CORE-15`, `CORE-16`, `CORE-17` in
> `backlog`, each with one faint line naming its spec section. A side branch from APPROVED,
> dashed: `request_rebaseline` → back to the grill, captioned "the current baseline governs
> until a new one is approved".
>
> Rules:
> - Approval is the grill finishing. Nobody sets it by hand.
> - Each item carries its section's framing, so the agent doesn't re-read the whole spec.
> - A change of scope reopens the grill. Closing a spec is final, and later work is a new spec.

### 04 · Memory

> Title "Memory." Lede: "Agents write down what the next agent should know. In the default
> review mode, nothing they write is trusted until you publish it."
>
> Illustration: three shard rows in Memory triage style, each with an origin chip and a
> status pill:
> `agent:auto-extract` "Lesson from CORE-12" `CANDIDATE` ·
> `agent:grill` "Decision from CORE-P3" `CANDIDATE` ·
> `user:alex` "API errors carry a hint field" `PUBLISHED`.
> One arrow from CANDIDATE through a `YOU · publish / reject` gate to PUBLISHED. A faint chip
> on the first row: `judge: not judged`.
>
> Rules:
> - Candidates stay out of agent search until they're published.
> - A judge scores candidates. Projects set to `auto` or `trusted` publish some without you.
> - A lesson pulled from a closed item always waits for you, in every mode.

### 05 · Code graph

> Title "Code graph." Lede: "Your agent describes the code it works in: modules, files and
> symbols, joined by imports, calls and tests. Agents read the graph before they read the
> tree."
>
> Illustration, left: a small graph. `api/` module → `api/export.py` file → two symbols,
> with typed edge labels `imports`, `calls`, `tested_by`. One node is drawn dashed and faint
> with the label `stale`.
> Right: a four-row scan table by path, one state each, in mono:
> `web/` `CLEAN AT 3f9c2a1` (green) · `api/` `HAS FINDINGS` (amber) ·
> `fleet/` `STALE` (muted, dashed) · `cli/` `NEVER SCANNED` (faint, dashed).
>
> Rules:
> - The graph is built on the machine that has the checkout. A node your agent didn't
>   re-describe is marked stale, not deleted.
> - Each scanner reports for a path at a commit. There's no overall score.
> - A missing scan isn't a clean one, and a stale result isn't either.

### 06 · Rules

> Title "Rules." Lede: "Your delivery process is a set of rules, each on the stage it
> governs. Every rule says what actually holds it, so a written rule never passes for an
> enforced one."
>
> Illustration, top: the seven stages as a thin flow, `Spec → Plan → Build → Review → Merge →
> Release → Learn`, each stage tinted at the level of its weakest used rule.
> Below: five rule rows, one per level, each with its level as an icon **and** a word (never
> colour alone):
> `ENFORCED` `review.not_own_work` "Not your own work": the server refuses ·
> `CHECKED` `merge.ci_green` "Merged work passed CI": CI reports, nothing refuses ·
> `EXTERNAL` `plan.budget_cap` "Budget cap": held outside the server, "Graphban can't verify
> this" · `ADVISORY` `gitops.base_branch` "Base branch": told to agents in every
> `get_context`, nothing refuses · `UNMEASURED` `gitops.pr_title_pattern`: no value set.
>
> Rules:
> - A stage reads as strong as its weakest rule. Advisory is never drawn as enforced.
> - A person turns a rule on or off, with a reason. A recommendation never does.
> - A refusal names the rule that refused it, or says it isn't mapped yet.

### Go deeper

> Title "Go deeper." Lede: "That's the short version. The concepts page covers why each idea
> exists and how they fit together."
>
> One link card, the whole width of the body: `graphban.dev/concepts` in mono, the line "The
> core ideas behind agent-first delivery, with the reasoning and worked examples.", then the
> six concept names as faint mono chips. Under the card, muted: "Opens in a new tab. Your
> setup stays where you left it." No rule lines. No sign-up, no newsletter, no second link.

---

## Part C — Public concepts page (graphban.dev/concepts)

Its own prompt. **Do not** paste P67 Parts 1 and 2: this page is not the app, has no tier
chips, and no seed project. One `Concepts.dc.html`, preview 1440×900, plus a 390px-wide frame.

> **Page: Graphban concepts.** A public, linkable page at `graphban.dev/concepts`. It's the
> long form of Quick Start's six concepts, and it doubles as marketing: someone who has never
> installed Graphban should come away knowing what it does differently.
>
> **Audience and tone.** Engineers who already run coding agents and have seen them collide,
> mark their own work done, or forget last week's decision. Precise, short sentences, no hype.
> Never "revolutionary", "magic", "10x", "seamless" or "autonomous". Every claim is something
> the product does today. Where a feature has a limit, say the limit.
>
> **Visual system.** Graphban's tokens, dark: canvas `#0a0b0d`, surfaces `#0f1317` /
> `#12171b`, hairlines `#2a333a`, text `#e6e9ec`, muted `#8b949e`, lime `#c6f24e`, purple
> `#a78bfa`. IBM Plex Sans; IBM Plex Mono for keys, commands, tool names and states. The
> tracker's six state colours where states appear. One max-width column of 1080px for the
> text; illustrations may run wider. Each concept's illustration reuses the Quick Start
> drawing for that concept, at a larger size, labelled `EXAMPLE`.
>
> **Structure, in this order:**
>
> 1. **Header.** Wordmark "Graphban" left. Right: "Concepts" (current), "Docs", "GitHub". No
>    pricing link, no log-in.
> 2. **Hero.** Title "Agent memory. Linear execution." Under it: "Graphban is a tracker your
>    coding agents use over MCP, through the same code path your clicks do. These are the six
>    ideas it's built on." A row of six anchor chips, one per concept.
> 3. **Six concept sections**, each in the same shape: a mono kicker `01 · THE LOOP`, a title,
>    **What it is** (2–3 sentences), **Why** (2–3 sentences), the illustration, and **What it
>    isn't** (one or two lines). Copy below.
> 4. **Closing band.** "Runs on your machine." `docker compose up` in a command block with
>    Copy, then "No API keys, no external services. The default model is an offline stub,
>    and you can switch to Ollama or Claude later." Two buttons: **Read the docs** (lime) and
>    **View on GitHub** (ghost).
> 5. **Footer.** Licence line: "Functional Source License 1.1. Each release becomes Apache 2.0
>    after two years." A © line. Nothing else.
>
> **Concept copy (use as written):**
>
> **01 · The loop.** *What it is:* You say what done means. One agent builds the work and
> moves it to review. A different agent checks the evidence and signs it off, or bounces it
> with the reason it failed. *Why:* An agent that grades its own work will pass it. Splitting
> builder and reviewer, and making sign-off cite evidence and the reviewed commit, makes
> "done" a claim someone else checked. *What it isn't:* Not a human approving every diff.
> Working alone, review waits for you or for a second agent.
>
> **02 · The tracker.** *What it is:* Every piece of work is an item in one of six states:
> backlog, next, in progress, review, blocked, done. You see them as a board or a list.
> Agents claim ready work, and a cluster claim takes related items together and reserves the
> files they touch. *Why:* Parallel agents collide on files, not on tickets. Leases and
> heartbeats mean a crashed agent's work is freed automatically instead of sitting claimed
> forever. *What it isn't:* Not a queue you assign by hand. Nobody hands an agent work in a
> chat.
>
> **03 · Specs.** *What it is:* Work that needs a decision starts as a spec. The grill asks
> questions until every section is testable, and finishing the grill is what approves it.
> Then the spec splits into items, each carrying its section's framing. *Why:* Agents guess
> at intent when it isn't written down. A grilled spec is the intent, and changing scope
> reopens the grill instead of quietly moving the target. *What it isn't:* Not a document
> anyone can mark approved. Closing a spec is final, and later work is a new spec.
>
> **04 · Memory.** *What it is:* Agents write down what the next agent should know:
> decisions, lessons, conventions. Each note starts as a candidate and stays out of agent
> search until it's published. *Why:* One confident, wrong note would otherwise become ground
> truth for every agent after it. Lessons pulled from closed items always wait for a person,
> because an item's description is written before the work and can be contradicted by what
> shipped. *What it isn't:* Not a judge deciding what's true. A model can score candidates.
> In the default mode, publishing is yours.
>
> **05 · Code graph.** *What it is:* Your agent describes the code it works in: modules,
> files and symbols, joined by imports, calls and tests. Agents search the graph before they
> grep the tree. Scanners report per path, at a commit. *Why:* Reading the graph once replaces
> a crawl, and it shows what a change touches before the change starts. A node the agent
> didn't re-describe is marked stale, not deleted, so the map doesn't rot into confident,
> wrong structure. *What it isn't:* No overall health score. A missing scan isn't a clean one,
> and a stale result isn't either.
>
> **06 · Rules.** *What it is:* Your delivery process, from spec to release, drawn as stages
> with the rules that govern each one. Every rule shows what holds it: the server refuses
> (enforced), CI or a scan reports (checked), something outside Graphban is supposed to
> (external), agents are only told (advisory), or nothing is set (unmeasured). *Why:* A rule
> written in a doc looks the same as one the server enforces, until an agent ignores it. Drawing
> every rule at the level that really holds it, and every stage at its weakest rule, keeps a
> hope from passing for a guarantee. *What it isn't:* Not a policy engine that turns itself
> on. A person adopts a rule, with a reason. A recommendation never changes enforcement.
>
> **Do not draw:** testimonials, customer logos, user counts, star counts, a pricing table, a
> sign-up form, a chat widget, a cookie banner, or a screenshot that implies features beyond
> these six.

---

## Part D — What to teach, and where

Every concept below is backed today. "Where" is where it's taught first. Quick Start gets
only what a first-run user meets in their first hour. The public page gets the six plus the
principles. Anything else lives in the tour or the docs.

### Core (Quick Start and the public page)

| # | Concept | The one idea | Source |
| --- | --- | --- | --- |
| 1 | The loop | Builder and reviewer are different agents. Sign-off cites evidence and a commit. | `graphban-review`, `tracker.md` |
| 2 | The tracker | Six states, board or list. Claims hold leases and files. | `tracker.md`, GRPH-1144 |
| 3 | Specs | Approval is the grill finishing. Specs decompose into items. | GRPH-P61 §3, `graphban-prd` |
| 4 | Memory | Candidate → published. Lessons from closed items always wait for a person. | `memory-and-chat.md`, `rules.md` |
| 5 | Code graph and scans | Agents describe the code. Stale isn't deleted, and missing isn't clean. | `mcp.md`, `graphban-scan` |
| 6 | Rules | Every rule shows what holds it. A stage is as strong as its weakest rule. | GRPH-P61, `rules.md` |

### Principles (public page, as a closing "How it's built" band; not in Quick Start)

| Concept | The one idea | Source |
| --- | --- | --- |
| One code path | An agent's MCP call and a person's click run the same service code. | `design-philosophy.md` §1 |
| Absence isn't clean | Unknown, unmeasured, stale and never-reported are drawn as themselves, never as a pass. | P67 §2, P61 §2.2, `graphban-scan` |
| Authority stays human | Signing in, minting a key, publishing memory and adopting a rule are a person's acts. A key never outranks the person who minted it. | `design-philosophy.md` §2, GRPH-P63 D10 |
| Every change is on the record | Activity holds one row per accepted change, attributed to a key or a person. | `design-philosophy.md` §2 |
| Errors teach the next step | Typed error codes with a hint that names the fix. | `design-philosophy.md` §3 |

### Second layer (docs and the tour; a later "Going further" page)

| Concept | The one idea | Source |
| --- | --- | --- |
| Proof before review | Both database engines, a sabotage receipt that shows the test can fail, one read of the live surface. | `graphban-prove` |
| Test scope | Where the suites run (`agent_full`, `agent_and_pr`, `agent_targeted`) is a per-project rule. | `rules.md` `build.test_scope` |
| Waits for decisions | An agent that hits an unstated product decision files a `wait:decision` item and stops. | `graphban-work` |
| Delegation and fleets | A parent hands one item to a child on another model. The result comes back as the item's state, not a chat reply. | `delegation.md` |
| Requests and triage | Feedback and requests arrive in triage, with duplicates detected, before they become items. | `README.md` |
| Local-first and the org | A box keeps its code graph local. Linking to an org adds shared house rules, cross-project memory, one roster and capacity routing. | GRPH-P67 §5, GBC-P9 |

### Not yet

- **Oversight profiles** (Exploratory, Standard, Regulated) and **org-level rule locks**
  (GRPH-P63 D13, D4) are org-plane, plan-gated features. Teach them in the cloud console tour
  once they're on screen, not on the public concepts page.

---

## The cloud console doesn't get this section

An org admin in the cloud console doesn't claim, review or publish memory, so these concepts
don't belong there. What an admin needs is how org and box relate: a box pushes into the org,
house rules and shared memory come down from it, and capacity is routed across the org's
entitlements. GBC-P9 S2 already has a tour of the console rail with a Guide control to replay
it. That's where this goes, not into a concepts deck.
