# Graphban Cloud Console — Claude Design prompt set (PRD-21)

Supersedes the roster and screen prompts in `cloud-tenant-design-prompts.md`. **Part 1's visual
system there is unchanged and reproduced below** — PRD-21 §1.2 is explicit that the token set
stands and this is not a re-theme.

What changed is *what gets drawn*: PRD-21 introduces a two-level hierarchy, a cross-repo galaxy,
teams, and a deployment identity model — and it **withholds seven screens** the old set specified
that nothing in the codebase backs.

**Workflow:** paste **Part 1** verbatim at the top, then **one** screen prompt from **Part 3**
beneath it. One `*.dc.html` per screen.

---

## Part 1 — Shared preamble (paste at the top of every screen)

> **Product.** Graphban is an agent-native dev tool: a linear tracker + pgvector agent memory +
> request triage + a code-structure graph, all operable by coding agents through MCP tools. It
> ships **local-first** (a self-hosted box builds the code graph on the dev's machine) and
> **cloud-hosted** (a multi-tenant SaaS). You are designing the **cloud console**.
>
> **How local and cloud actually divide — get this right, it drives three screens.** When a local
> box is *linked*, agents talk to the local MCP endpoint, but **only the code-graph tools run
> locally**. Items, claims, leases, memory, PRDs and backlog are all **forwarded to the cloud**,
> which stays authoritative for mutable contended state. So the cloud already knows which agents
> are running and what they hold — it is a query, not an embed. The local box pushes its code
> graph *up* as summaries and structure only; the cloud re-embeds.
>
> **Nouns — model these exactly:**
> - **Org / tenant** — a hosted account. Owns projects, teams, members, billing. Roles rank
>   **owner > admin > member**; the owner is the creator and cannot be demoted.
> - **Project** — one unit of work = **one pushed graph = one sync credential = one checkout**. A
>   monorepo is ONE project that publishes several package names.
> - **Team** — a named group with **grants** (project + access level). A grant is the unit of
>   access administration.
> - **Linked deployment** — a self-hosted box. **Its identity IS the sync credential** pasted into
>   `graphban link`. One key = one deployment. The cloud authenticates keys, not machines.
> - **Operator** — cross-tenant platform admin. A separate plane, visually distinct, invisible to
>   tenants.
>
> **The two-level hierarchy — the nav follows it literally:**
> - **Org level** `/org/*` — Overview, Galaxy, Projects, Teams, Deployments, Members, Invites,
>   API keys, Billing. (Integrations and Analytics exist in the rail for layout; see Part 2.)
> - **Project level** `/p/:tag/*` — the project's own app: Code graph, PRDs, Tracker, Triage,
>   Memory, Agents.
> - An **org switcher** sits at the top of the rail; a **project breadcrumb** appears once you are
>   inside `/p/:tag`. Drilling from Projects into a project is the primary motion of the product.
>
> **Visual system (match exactly):**
> - Dark canvas `#0d0f0e`; surfaces `#111412` (cards) and `#0b0d0c` (insets/inputs); borders
>   `#20241f` (strong) / `#1b1f1a` (subtle).
> - Type: **IBM Plex Sans** for UI, **IBM Plex Mono** for labels, values, IDs, codes, paths, and
>   any tabular data. Micro-labels are mono, 10px, UPPERCASE, letter-spacing ~.7px, color
>   `#5b6355`.
> - Text: primary `#e6e9e4`, secondary `#dfe4da`, muted `#868f80`, faint `#5b6355`, faintest
>   `#4e564a`.
> - Accent **lime** `#c6f24e` (hover `#d8ff74`) — primary actions, active nav, selection bars.
> - Status palette: green `#5fd07a` (healthy / in-sync / done), amber `#e2b247` (stale / warning /
>   review), blue `#7ca2ff` (next / info), red `#e85d5d` (error / danger), purple `#a78bfa` /
>   `#c9b8ff` (agent / AI).
> - Idioms: rounded cards (`border-radius:13px`, 1px border, `#111412` fill, 16px padding);
>   section headers = small icon + 14px semibold title + optional mono chip + right-aligned status
>   pill; **status pill** = a 6px dot + mono 10px uppercase label in a tinted-border pill; inset
>   tables use mono values under mono uppercase column headers; left nav rail ~200px with a mono
>   section eyebrow and a dot on the active item; footer build stamp in tiny mono.
> - Icons: thin line icons (1.8 stroke), lime or contextual.
> - Tone: precise, engineer-facing, quietly confident. Real-looking data, never lorem ipsum.
>
> **The console never reaches into a local box.** Nothing is framed, proxied, relayed or
> tunnelled. Where a screen offers the developer their own deployment, it renders the **address**
> and links it to a new tab — reachability depends on the viewer's network and cannot be tested
> from here, so the address is shown rather than hidden behind a button that might dead-end.
>
> **Two more rules that are product decisions, not styling — apply them on every screen:**
> 1. **An empty state must say which kind of empty it is.** "Nothing here" and "nothing has been
>    looked at yet" are different facts and must never render the same way. Where a screen has
>    more than one empty state, the prompt names them; draw each.
> 2. **Nothing disappears silently.** Data that has gone stale renders **faded and dashed with its
>    own age** ("last seen 4 pushes ago"), never removed. There is no auto-purge anywhere in this
>    product.
>
> **`.dc.html` conventions:** wrap in `<x-dc>`; fonts + a `<style>` reset in `<helmet>`; use
> `<sc-for list="{{ rows }}" as="r" hint-placeholder-count="N">` for lists and
> `<sc-if value="{{ flag }}">` for conditionals; bind with `{{ }}`; wire events with
> `onClick="{{ handler }}"` and hover with `style-hover="…"`. Drive it from a
> `<script type="text/x-dc" data-dc-script data-props="{&quot;$preview&quot;:{&quot;width&quot;:1160,&quot;height&quot;:900}}">`
> block containing `class Component extends DCLogic { state = {…}; renderVals(){ return {…}; } }`.
> Include realistic seed data and at least the interactive state the screen hinges on.

---

## Part 2 — Roster

**P0 — the core loop.** 1 Org overview · 2 Super galaxy · 3 Projects · 4 Linked deployments ·
5 Mint sync credential · 6 Project home · 7 Code graph with arrows out

**P1 — governance and the PM surface.** 8 Teams · 9 Members & roles · 10 Invites · 11 API keys ·
12 Billing & usage (display only) · 13 PRD workspace · 14 Triage · 15 My agents

**P2 — edges and the operator plane.** 16 Public reporting page · 17 Accept invite · 18 Create org ·
19 Operator home · 20 Operator orgs · 21 Operator users · 22 Operator licensing

**Speculative — draw, but label.** 23 Integrations · 24 Analytics. These ship *hidden* until
the integrations and analytics PRDs land. They are designed now so the rail composes once and never needs re-cutting.
**Mark each with a mono `SPECULATIVE — NOT BACKED` chip in the page header** so no one mistakes a
mockup for a specification.

### Do not design these

Seven screens in the old set describe capability that does not exist. Drawing them produces
beautiful mockups nobody can wire.

| Screen | Why not |
| --- | --- |
| Cross-repo triage / collision clustering | Collision clustering is **per-project** in code. Cross-repo does not exist. Screen 14 is the per-project one, labelled honestly. |
| Security / SSO / SAML / data residency | No code of any kind. |
| Billing — payment method, invoices, upgrade flow | No Stripe. Plans are operator-assigned. Screen 12 is the **display** half only. |
| Usage spike charts / end-of-month projection | Usage is one row per period. No time series exists to chart. |
| Operator suspend / restore / impersonate | No endpoints. Operator screens are **read-only**. |
| Operator audit log | Events are project-scoped; no cross-tenant read exists. |
| Member role change / removal | ← **exception: this one IS built in PRD-21.** Design it (screen 9). |

---

## Part 3 — Screen prompts

### 1 · Org overview  *(P0)*
> **Screen: Org overview.** The tenant's home — one glance across every project. Page header: org
> name + plan chip. A row of four **stat cards**: active items, open claims/leases, projects in
> sync ("5 / 7"), MCP calls this month against the monthly quota (thin bar, amber near the cap).
> Below, two columns: left = **per-project health** (name with accent dot, item counts by status,
> code-graph node count, a sync pill IN SYNC / STALE / NEVER, last push relative); right =
> **recent activity** (agent claims, pushes, item transitions — mono actor handles, tiny
> timestamps). A **collision watch** callout when two in-flight items in the *same* project overlap
> on code (amber) — name the project ("in `GRPH`"), never imply cross-repo.
> **States:** loading (skeleton cards); **brand-new org** (no projects → one nudge: "link your
> first deployment", pointing at screen 5); healthy; near-quota (amber bar).

### 2 · Super galaxy  *(P0 — the flagship)*
> **Screen: Super galaxy.** The reason the org level exists: **how this org's repos relate to each
> other.** A force-directed graph filling the pane. **Nodes are projects**, sized by code-graph
> node count, labelled with the project name in mono, tinted by accent. **Edges are evidenced
> dependencies** between repos, weighted by how many facts support them.
> The load-bearing interaction: **hovering an edge shows its evidence** — a small mono popover
> listing the file and the fact found there, e.g. `web/package.json → @acme/core ^2.1`. Every edge
> in this product can name the file that proves it; the UI must make that visible, because it is
> the difference between this graph and a guess.
> **Stale edges** (a dependency no longer declared on the last push) render **faded and dashed**
> with "last seen 4 pushes ago" in the popover, behind a `SHOW STALE` toggle. They are never
> removed.
> Controls: pan / zoom / drag, a **find** box, and a filter row (edge type: depends on · serves ·
> declared). Clicking a node opens that project. Selecting a node dims everything more than one
> hop away.
> **Three distinct empty states — this is the point of the screen, get all three:**
> (a) **no projects** → the "create a project" nudge;
> (b) **projects but nothing pushed yet** → *draw the project nodes, unconnected*, with a banner
> "no deployment has pushed a manifest yet" — the org is not empty, the **edges** are;
> (c) **projects pushed, no internal dependencies** → nodes drawn, banner "every dependency
> resolved to an external package" — a legitimate answer that must **not** look like an error.
> Also show a **name collision** warning strip when two projects claim the same package name:
> "`@acme/core` is claimed by 2 projects — no edge drawn."

### 3 · Projects  *(P0)*
> **Screen: Projects.** A table of the org's projects: name (accent dot), the package names it
> **publishes** (mono chips — this is the registry the galaxy resolves against), members count,
> item count, code-graph node count, sync pill + last push, row action (open / settings). Primary
> **New project** button opens an inline panel: name, description, accent swatch, and the tag that
> will appear in its URL (`/p/GRPH`) shown live as you type — tags are 2–4 uppercase characters,
> so validate as they type. A note that a fresh project is empty
> until a linked deployment pushes to it. Surface any **publishes-name collision** inline on the
> offending rows.
> **States:** empty org (one "create your first project" card), populated, mid-creation.

### 4 · Linked deployments  *(P0)*
> **Screen: Linked deployments.** Which local boxes push into this tenant. Header line: "Local
> deployments push their code graph here using a sync credential. **The credential is the
> deployment's identity.**"
> Table: label (the sync key's name — say so), target project, the masked key (`gb_sk_…`), last
> push relative + node count, freshness pill IN SYNC / STALE / NEVER, the self-reported **address**
> as mono text (linked, opens a new tab — never framed or probed), and a **revoke** row action
> (danger).
> Three things this screen must communicate that a naive key list would not:
> (a) **one key = one deployment** — if one key is used on two machines they appear as one row,
> and the screen says so rather than implying two;
> (b) a **retired** deployment (key rotated or revoked) stays visible, faded, marked RETIRED —
> never silently removed;
> (c) a row whose **hostname changed** under a stable key gets an amber flag reading "hostname
> changed — was `laptop-01`", because that is a credential-movement signal worth seeing.
> A secondary **Sync credentials** section lists keys minted for inbound sync with their pinned
> project, created / last-used, and a **Mint sync key** button (→ screen 5). Privacy line:
> "Summaries and structure only — vectors are re-embedded here."
> **States:** none linked (explain how to link from a local box); one healthy; one stale (amber);
> one errored auth (red); one retired (faded).

### 5 · Mint sync credential  *(P0)*
> **Screen: Mint sync credential** (focused panel or modal). Fields: **key name**, target
> **project** (a sync key pins to exactly one), optional expiry. Because the key is the
> deployment's identity, **the name becomes the deployment's label everywhere in the console** —
> say that at the point of naming, with an example ("laptop — acme-core"). Callout that the key is
> shown once. On create: success panel with the key in a mono box + copy, and the ready-to-paste
> command `graphban link --cloud-url … --api-key gb_sk_… --project …`. Emphasize least privilege:
> this key can push a code graph to its one project and nothing else.

### 6 · Project home  *(P0)*
> **Screen: Project home** at `/p/:tag`. The landing pad after drilling in from Projects. Header:
> project name + accent + tag chip (mono, e.g. `GRPH`) + sync pill, and a breadcrumb back to the
> org. A compact row of counts (items by status, memory shards, graph nodes, last push). Then
> section cards linking into the project's own surfaces: **Code graph**, **PRDs**, **Tracker**,
> **Triage**, **Memory**, **Agents**. A small **depends on / depended on by** strip listing sibling
> projects with the evidence count, linking to the galaxy.
> **States:** healthy; never-synced (the graph card explains what to do); no agents active.

### 7 · Code graph, with the arrows out  *(P0)*
> **Screen: Project code graph.** The existing interactive graph — pan, zoom, drag, find, pinned
> nodes, agent-presence clouds tinting nodes an agent currently holds — plus the PRD-21 addition:
> **dependencies that leave the repo.**
> A dependency on a sibling project renders as a distinctly-shaped **project stub node** carrying
> the sibling's name, connected to the real file that declares it (e.g. the `web/package.json`
> node). Two affordances, and they differ: clicking the **stub** opens that project's code graph;
> clicking the **edge** opens the galaxy focused on that relationship. The edge's tooltip is the
> evidence string — the file and the fact.
> Include a node-kind legend (module · file · symbol · doc · config) and a fleet legend showing
> which agent's colour is which.
> **States:** populated with two outbound stubs; a stale node (faded); no graph pushed yet.

### 8 · Teams  *(P1)*
> **Screen: Teams.** Left: list of teams (name, member count, grant count). Right: the selected
> team — its **members** (avatar, name, mono handle, remove) and its **grants** (project + access
> `WRITE` / `READ`, add / change / revoke). A line explaining the model plainly: "A grant gives
> every member of this team access to that project." Show, on a grant row, how many memberships it
> currently materializes ("grants access to 6 people").
> The honest detail: when a user **already has direct access** to a granted project, show them in
> the grant's expansion marked `DIRECT` and note that revoking the team grant will not remove it.
> **States:** no teams (one "create a team" card); a team with no grants; a team whose grant
> overlaps direct memberships.

### 9 · Members & roles  *(P1)*
> **Screen: Members.** Table: avatar + name + mono handle, org role pill OWNER / ADMIN / MEMBER,
> project access summary, last active. Row actions — **change role** and **remove** — with the
> owner row's actions disabled and a tooltip saying the owner cannot be demoted. Seat usage near
> the header ("8 / 15 seats"), amber when full. A member whose access comes from a team shows a
> team chip and a note that it is managed by that team.
> **States:** just-you (new org); populated; at seat cap (invite disabled with an upgrade hint);
> a confirm dialog for removal naming exactly what is lost.

### 10 · Invites  *(P1)*
> **Screen: Invites.** Send panel: email, role select, project access. Table of pending invites
> (email, role, invited by, expires in, resend / revoke) and a collapsed accepted/expired history.
> Note the delivery model: an emailed link, with the accept URL copyable from this screen so the
> flow works even when mail delivery is unconfigured. **States:** none pending; several pending;
> one expired (faint).

### 11 · API keys & credentials  *(P1)*
> **Screen: API keys.** Two clearly separated groups: **Agent keys** (scopes read/write, project
> pin or org-wide, last used, expiry, revoke) and **Sync credentials** (scope `sync`, pinned to one
> project, in use by a linked deployment — cross-link to screen 4). Create panel: name, scope
> checkboxes, project scope, expiry — with a `sync` scope forcing a project selection, since a sync
> key must pin to one. New key shown once in a mono box with copy + a connect snippet.
> **States:** no keys; mixed; an expired key (red); a revoked one (faded).

### 12 · Billing & usage — display only  *(P1)*
> **Screen: Billing.** Current plan card (tier name, what it includes) and a **limits vs usage**
> table with four rows only, because these are the four counters that exist: **projects**,
> **seats**, **memory shards**, **MCP calls this month** — each a used/limit pair with a thin bar,
> amber as it nears the cap. A plan comparison row (Free / Pro / Team / Enterprise) showing those
> same four limits per tier.
> **Design no payment method, no invoice list, and no self-serve upgrade button.** Plans are
> assigned by an operator today. The upgrade affordance is a single line: "Contact your operator
> to change plan." Do not draw a usage chart — there is no time series behind it.
> **States:** comfortable; one counter near cap (amber); one at cap (red, with what is now blocked).

### 13 · PRD workspace  *(P1)*
> **Screen: PRD workspace** at `/p/:tag/prds/:id`. The PM's surface, and the loop is: draft →
> **grill** → approved → **decompose** into tracked items. Left: the PRD body rendered with its
> section headings. Right: a **grill panel** — the interrogation, as a conversation of questions
> and the author's answers, with a progress strip showing which dimensions are still outstanding
> (contracts · failure modes · open decisions · scope edges) and a prominent state chip that reads
> `APPROVED — reached by finishing the grill` once complete. Below it, **coverage**: per-section
> task counts and bars, with sections that have no tasks called out as gaps, and a **Decompose**
> action. An intent-diff strip showing what changed since the last approved version.
> **States:** draft with the grill unstarted; grill mid-flight with 2 dimensions outstanding;
> approved with 11 sections and 0 decomposed; approved and fully decomposed.

### 14 · Triage  *(P1)*
> **Screen: Triage** at `/p/:tag/triage`. **Per-project — label it as such in the header.** Left
> column: incoming requests awaiting triage (title, source, votes, duplicate hints). Main area:
> **collision clusters** over in-flight work in this project — each card lists the items whose
> code touch-areas overlap, the shared paths as mono chips, a risk pill, and a recommendation
> ("serialize — both touch `services/code_graph.py`" / "safe to parallelize"). Expanding a cluster
> shows the member items and the overlap that bound them.
> **States:** all clear (calm green); one high-risk overlap (red); triage queue empty.

### 15 · My agents  *(P1)*
> **Screen: Agents** at `/p/:tag/agents`. Which agents are working this project *right now* —
> and note in the header that this is **read from the cloud**, because a linked deployment forwards
> its claims here. Table: agent handle (mono, coloured), role, status pill ONLINE / IDLE / OFFLINE
> derived from last contact, what it holds (item id + title), lease remaining as a small countdown,
> last heartbeat. A held-areas list showing the paths each agent has reserved.
> Below, a **local deployment** card. It **does not embed anything** — it shows the address the
> deployment reported (`http://ubuntu-srv:8080`) as readable mono text, and links it to open in a
> new tab. Render the address rather than hiding it behind an "Open" button: the console cannot
> test whether that address is reachable from *this* viewer's network, so showing it lets someone
> on the wrong network see that before they click, where a dead-ending button would not. Include a
> small "change address" affordance — the same box answers differently from different networks.
> **States:** two agents active with one lease expiring (amber); nobody working; a deployment that
> reported no address at all (the card explains how to set one, and the rest of the screen is
> unaffected).

### 16 · Public reporting page  *(P2)*
> **Screen: Public report.** The unauthenticated per-project feedback page at `/r/<token>`, served
> outside the org URL entirely. A centered, minimal card carrying the project's accent: a short
> heading, a description field, an optional email, an attachment control, and a submit. As the
> user types, show **live duplicate hints** — existing requests that look similar, each with a
> vote affordance so a duplicate becomes a vote instead of a new row. Confirmation state showing
> what was filed. No org chrome, no nav rail, no sign-in prompt.
> **States:** empty form; duplicates surfaced mid-typing; submitted; a project that has not opted
> into public sharing (a plain not-found — it must not reveal that the project exists).

### 17 · Accept invite  *(P2)*
> **Screen: Accept invite.** Centered card, no shell. The org you are invited to, who invited you,
> the role you will receive, the account you are accepting as, one primary **Join** action and a
> decline link. **States:** valid; expired ("ask [inviter] to resend"); already a member.

### 18 · Create org  *(P2)*
> **Screen: Create org.** First-run, centered. Org name + accent, your role (owner, fixed), an
> optional invite-teammates field, and a closing card teasing the obvious next step — **link a
> deployment** — so a brand-new empty tenant has one clear action. **States:** form; submitting;
> done with the link-a-deployment CTA.

### 19 · Operator console home  *(P2)*
> **Screen: Operator console.** The platform-admin plane — visibly distinct from tenant UI (cooler,
> darker chrome, a mono `OPERATOR` badge) so it is never mistaken for a tenant view. Platform
> health: total orgs, users, linked deployments, aggregate MCP throughput. Left rail: Orgs, Users,
> Licensing. **Read-only** — this plane observes and provisions; it does not reach into tenants.
> **States:** healthy; an org over quota (amber).

### 20 · Operator — orgs  *(P2)*
> **Screen: Operator / Orgs.** Every tenant: org name, owner (mono handle + email), plan, seats
> used, projects, MCP usage vs quota, created. Row opens a detail drawer showing members and usage,
> plus the one mutation an operator has: **assign plan**. **No suspend, no restore, no
> impersonation** — those do not exist. Search and filter by plan. **States:** populated; one org
> at quota; plan-assign open.

### 21 · Operator — users  *(P2)*
> **Screen: Operator / Users.** Platform-wide users: name + handle, email, org memberships as
> chips, last active. **Read-only** — search and inspect, no disable, no password reset.
> **States:** populated; a user in three orgs.

### 22 · Operator — licensing  *(P2)*
> **Screen: Operator / Licensing.** Operator-issued **platform invites** that let someone found a
> new org: mint an invite with the plan it grants and an expiry; a table of issued invites
> (recipient, plan, status, copy accept-link, revoke). Plus the platform **signup mode** control
> (open / invite-only / closed) with each mode explained inline. Also a queue of
> **additional-org requests** from existing users, each approve/decline.
> **States:** invite-only mode with three outstanding invites; one redeemed; one pending org
> request.

### 23 · Integrations  *(SPECULATIVE — PRD-23)*
> **Screen: Integrations.** Header carries a mono `SPECULATIVE — NOT BACKED` chip; this ships
> hidden until PRD-23. A grid of connector cards — **Jira, Linear, Confluence, Trello**, plus the
> two that already exist (**GitHub**, **Google Drive**) — each with a connect action, connected
> state showing the account and target, and a per-project mapping row. A note that a connected
> tracker raises a question of authority (which system owns an item's status) that PRD-23 settles.
> **States:** none connected; GitHub connected; a connector mid-auth.

### 24 · Analytics  *(SPECULATIVE — a later PRD)*
> **Screen: Analytics.** Header carries the same `SPECULATIVE — NOT BACKED` chip; ships hidden
> until that PRD lands. Sections sketching what a program manager needs: **usage** across projects, **model
> usage** by project, **burndown against PRDs**, and a **standup** digest of what each agent and
> person moved. Draw it as the target shape, not as a spec — the metering behind model usage does
> not exist yet and the analytics PRD has to decide whether to build it.
> **States:** populated; a project with no data.

---

## Notes for whoever runs the set

- **Design 1–7 first.** That is org → galaxy → project → graph, the drill-down that is the whole
  argument for having an org level. If those seven feel right, the rest is tables.
- **Reuse components ruthlessly.** The status pill, the stat card, the freshness row and the
  evidence popover should be drawn once and then referenced by name in later prompts.
- **The galaxy is the screen to spend time on.** Everything else in this console exists in some
  form in other products; a repo-level dependency graph whose every edge can name the file that
  proves it does not.
- **When a prompt names three empty states, draw three.** They are different facts, and rendering
  them identically is the specific failure this product is built to avoid.
