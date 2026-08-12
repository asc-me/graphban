# Graphban Cloud Tenant — Design Prompt Set

A reusable set of **Claude Design prompts** for the hosted **Cloud Tenant** UI. Each prompt in
Part 3 produces one `*.dc.html` screen. Every generated screen must share one visual language and
one product frame — so the workflow is: paste the **Part 1 preamble** once at the top, then paste
the **one screen prompt** you want beneath it.

> Perspective reminder (the thing that trips people up): the **cloud tenant** is the *hosted org
> account* — it owns projects, items, claims, memory, and receives code graphs pushed **up** from
> **local self-hosted deployments**. This prompt set designs the **cloud** side. The **local**
> deployment's Settings → Sync/Link page (already built) is the counterpart that looks *up* at one
> of these tenants. Don't conflate **instance/deployment** (a running box), **org/tenant** (a hosted
> account), and **project** (a unit of work inside an org).

---

## How to use

1. Open Claude Design, new file `<Screen Name>.dc.html`.
2. Paste **Part 1 — Shared preamble** (verbatim).
3. Paste **one** screen prompt from **Part 3** below it.
4. Generate, then iterate on states (empty / loading / error) — the prompts name them, but the first
   pass usually renders the happy path; ask for the others explicitly.
5. Keep the roster in **Part 2** as the checklist of what "the whole Cloud Tenant UI" covers.

Consistency rules of thumb: reuse the tokens and idioms from the preamble; a left rail for
top-level nav; mono micro-labels; status pills; content column ~640–760px; never invent a fifth
accent color.

---

## Part 1 — Shared preamble (paste at the top of every screen)

> **Product.** Graphban is an agent-native dev tool: a linear tracker + pgvector agent memory +
> request triage + a code-structure graph, all operable by coding agents through MCP tools. It ships
> **local-first** (a self-hosted box builds the code graph on the dev's machine) and **cloud-hosted**
> (a multi-tenant SaaS that holds items, claims, memory, and triage/collision-clustering across a
> whole org). Local deployments **push** their code graph *up* to a cloud tenant — summaries and
> structure only, never vectors; the cloud re-embeds.
>
> **You are designing the CLOUD TENANT UI** — the hosted, multi-tenant product. Model these nouns
> exactly:
> - **Org / tenant** — a hosted account. Owns projects, members, billing. Roles rank **owner >
>   admin > member**. The owner is the creator and can't be demoted.
> - **Project** — a unit of work inside an org. Has items, memory, a received code graph.
> - **Local deployment / linked instance** — a self-hosted box that links to this tenant with a
>   `sync`-scoped credential and pushes one project's code graph up.
> - **Operator (platform admin)** — a cross-tenant role for whoever runs the hosted platform. Its
>   console is a separate plane, only visible to operators.
>
> **Visual system (match exactly):**
> - Dark canvas `#0d0f0e`; surfaces `#111412` (cards) and `#0b0d0c` (insets/inputs); borders
>   `#20241f` (strong) / `#1b1f1a` (subtle).
> - Type: **IBM Plex Sans** for UI, **IBM Plex Mono** for labels, values, IDs, codes, and any
>   tabular data. Micro-labels are mono, 10px, UPPERCASE, letter-spacing ~.7px, color `#5b6355`.
> - Text colors: primary `#e6e9e4`, secondary `#dfe4da`, muted `#868f80`, faint `#5b6355`, faintest
>   `#4e564a`.
> - Accent **lime** `#c6f24e` (hover `#d8ff74`) — primary actions, active nav, selection bars.
> - Status palette: green `#5fd07a` (healthy / in-sync / done), amber `#e2b247` (stale / warning /
>   review), blue `#7ca2ff` (next / info), red `#e85d5d` (error / danger / destructive), purple
>   `#a78bfa`/`#c9b8ff` (agent / secondary-transport / AI).
> - Idioms: rounded cards (`border-radius:13px`, 1px border, `#111412` fill, 16px padding); section
>   headers = small icon + 14px semibold title + optional mono chip + right-aligned status pill;
>   **status pill** = a 6px dot + mono 10px uppercase label in a pill with a tinted border; inset
>   tables/rows use mono values with mono uppercase column headers; a left nav rail ~200px wide with
>   a small mono section eyebrow and a dot on the active item; footer build stamp in tiny mono.
> - Icons: thin line icons (1.8 stroke), lime or contextual color.
> - Tone: precise, engineer-facing, quietly confident. Real-looking data, never lorem ipsum.
>
> **`.dc.html` conventions:** wrap the screen in `<x-dc>`; put fonts + a `<style>` reset in
> `<helmet>`; use `<sc-for list="{{ rows }}" as="r" hint-placeholder-count="N">` for lists and
> `<sc-if value="{{ flag }}">` for conditionals; bind with `{{ }}`; wire events with
> `onClick="{{ handler }}"` and hover with `style-hover="…"`. Drive it from a
> `<script type="text/x-dc" data-dc-script data-props="{&quot;$preview&quot;:{&quot;width&quot;:1160,&quot;height&quot;:900}}">`
> block containing `class Component extends DCLogic { state = {…}; renderVals(){ return {…}; } }`.
> Include realistic seed data and at least the interactive state the screen hinges on.
>
> **Shell:** unless the prompt says otherwise, render the tenant shell — a left nav rail (Overview,
> Projects, Backlog, Triage, Code graph, Memory, Members, API keys, Billing, Settings), an org
> switcher at the top of the rail, and the named screen in the content pane.

---

## Part 2 — Workflow map (the full roster)

Priority: **P0** = core tenant loop, design first · **P1** = governance & billing · **P2** =
operator plane & polish.

### A. Onboarding & access
1. **Accept invite / join a tenant** — P1
2. **Create org (first-run tenant setup)** — P1
3. **Sign in** (email + SSO) — P2

### B. Tenant home
4. **Org overview dashboard** — cross-project health, sync freshness, activity, quota headroom — P0

### C. Projects & inbound sync (cloud side of the local Sync/Link page)
5. **Projects list** + create project — P0
6. **Project detail** (settings, members subset, memory/graph summary) — P1
7. **Linked instances / inbound sync** — which local deployments push here, freshness, sync keys — P0
8. **Mint sync credential** — create the `sync`-scoped key a local box will use — P0

### D. Work surfaces (cloud-held)
9. **Backlog / items** with **claims & leases** (who's holding what) — P0
10. **Triage & collision clustering** — the cross-repo reason the cloud exists — P0
11. **Code graph explorer** — the received graph, cross-project search — P1
12. **Memory browser** — org/project agent memory — P1
13. **PRDs** — P2

### E. People & governance
14. **Members & roles** — P1
15. **Invites management** — P1
16. **API keys & credentials** — agent keys + sync keys, scopes, lifecycle — P1
17. **Security** — SSO, BYOK provider keys, data residency — P2

### F. Billing
18. **Plan & billing** — tiers, seats, payment — P1
19. **Usage & quotas** — MCP burst cap, monthly quota, per-project usage — P1

### G. Operator plane (platform-admin, cross-tenant)
20. **Operator console home** — P2
21. **Orgs list** (all tenants) — P2
22. **Users list** (platform-wide) — P2
23. **Platform invites / licensing** — P2
24. **Operator audit log** — P2

---

## Part 3 — Screen prompts

Each prompt assumes the Part 1 preamble is already pasted above it.

### 4 · Org overview dashboard  *(P0)*
> **Screen: Org overview.** The tenant's home — a member's first glance at org health. Content column
> with a page header (org name + plan chip). Then a row of 4 **stat cards**: active items, open
> claims/leases, projects in sync (e.g. "5 / 7"), and MCP calls this month vs. cap (a thin usage bar,
> amber as it nears the cap). Below, two columns: left = **Sync freshness** list (each project: name,
> a status pill IN SYNC / STALE / PAUSED / ERROR, "last push" relative time, node count); right =
> **Recent activity** feed (agent claims, pushes, item transitions — mono actor handles, tiny
> timestamps). A **collision watch** callout if any two in-flight items overlap on code (amber). Empty
> state for a brand-new org: a "link your first deployment" nudge pointing at the sync-credential
> flow. States: loading (skeleton stat cards), empty, healthy, near-quota (amber bar).

### 5 · Projects list + create  *(P0)*
> **Screen: Projects.** A table of the org's projects. Columns: name (with a colored dot accent),
> members count, items count, **sync status** pill + last-push time, code-graph node count, and a
> row action (open / settings). A primary **New project** button top-right opens an inline panel or
> modal: name, description, accent color swatch picker, and a note that a fresh project is empty until
> a linked deployment pushes its graph. Show a **local-only vs mapped** distinction subtly if useful.
> States: empty org (one big "Create your first project" card), populated, and a row mid-creation.

### 7 · Linked instances / inbound sync  *(P0, mirror of the local page)*
> **Screen: Linked instances.** The cloud-side view of who pushes into this tenant — the counterpart
> to a local deployment's Sync/Link page. Header explains: "Local deployments push their code graph
> here using a sync credential." A table of **linked deployments**: a label/hostname, the project it
> targets, the **sync key** used (masked, `gb_sk_…`), last push (relative), pushed node count, a
> freshness pill (IN SYNC / STALE / NEVER), and an action to **revoke** the key (danger). A secondary
> section **Sync credentials**: keys minted for inbound sync with scope `sync`, project pin, created/
> last-used, and a **Mint sync key** button (→ screen 8). Include a privacy line: "Summaries and
> structure only — vectors are re-embedded here." States: no deployments linked yet (explain how to
> link from a local box), one healthy, one stale (amber), one errored auth (red).

### 8 · Mint sync credential  *(P0)*
> **Screen: Mint sync credential** (modal or focused panel). A short flow to create the `sync`-scoped
> org key a local deployment authenticates with. Fields: key name (e.g. "laptop — acme-core"), target
> **project** (select — a sync key pins to one cloud project), optional expiry. A callout that the key
> is **shown once**. On create: a success panel with the key in a mono code box + copy button, and a
> ready-to-paste **`graphban link`** command (`graphban link --cloud-url … --api-key gb_sk_… --project …`).
> Emphasize least-privilege: this key can only push a code graph to its one project, nothing else.

### 9 · Backlog with claims & leases  *(P0)*
> **Screen: Backlog.** The org/project item stream (linear tracker). A filter bar (status chips:
> Backlog / Next / In progress / Review / Done / Blocked; project scope select). Each item row: id
> (mono, e.g. `AL-141`), title, status pill, type tag, effort, and — the cloud-specific part — a
> **claim/lease** indicator: if an agent holds it, show the agent handle, a small lease timer/dot, and
> "claimed 12m ago"; unclaimed items show a faint "open". A right-side detail drawer on click:
> description, touchpoints, linked code, claim history. Emphasize the leasing model (agents claim work
> so parallel fleets don't collide). States: loading, empty backlog, an item actively leased, a lease
> about to expire (amber).

### 10 · Triage & collision clustering  *(P0 — the flagship)*
> **Screen: Triage.** The cross-repo intelligence that justifies the cloud. Show incoming
> **requests/feedback** being triaged into clusters, and **collision clustering** over in-flight work:
> a set of **clusters**, each a card listing the items that touch overlapping code (with the shared
> files as mono chips), a risk level (green/amber/red), and a recommendation ("safe to parallelize" /
> "serialize — both touch `services/code_graph.py`"). A left column of ungrouped incoming requests;
> the main area = clusters. Interaction: expand a cluster to see member items + the code overlap that
> bound them. This is the screen to make feel *smart* — the cloud reasoning across the whole graph.
> States: no collisions (calm green "all clear"), one high-risk overlap (red), triage queue empty.

### 11 · Code graph explorer  *(P1)*
> **Screen: Code graph.** Browse the received code graph for a project. A left tree/list of nodes
> (files/symbols, mono paths, kind + lang chips), a center detail for a selected node (its summary —
> the LLM describe output — plus neighbors: imports/imported-by as edge lists, and work items whose
> touchpoints hit it), and a **search** box up top (semantic — "where does auth live"). A small
> banner noting the graph was **pushed from a linked deployment** and re-embedded here, with last-push
> freshness. States: empty (no graph pushed yet → point at Linked instances), populated, search
> results.

### 12 · Memory browser  *(P1)*
> **Screen: Memory.** The agent memory store for the org/project. A list of memory entries (each: a
> title, a type chip — lesson / decision / reference / context, a source, a relative time, and a
> similarity/score when searched), with a semantic **search** bar and type filters. A detail drawer
> showing the full entry and what it links to. A subtle "global vs project" scope toggle (some memory
> is shared across projects). States: loading, empty, search with ranked hits.

### 14 · Members & roles  *(P1)*
> **Screen: Members.** People in the org. A table: avatar + name + mono handle, role pill (OWNER /
> ADMIN / MEMBER), access (write/read), last active. Row actions: change role (respecting owner >
> admin > member; owner immutable), remove. An **Invite** button → screen 15. Show seat usage vs plan
> ("8 / 10 seats") near the header, amber when full. States: just-you (new org), populated, at seat
> cap (invite disabled with an upsell hint).

### 15 · Invites management  *(P1)*
> **Screen: Invites.** Pending and past invites. Send-invite panel: email, role select, project
> access. A table of pending invites (email, role, invited-by, expires-in, resend / revoke) and a
> collapsed "accepted/expired" history. Note the delivery model (emailed link; falls back to a
> console/outbox when SMTP is unconfigured — useful for self-host). States: none pending, several
> pending, an expired one (faint).

### 16 · API keys & credentials  *(P1)*
> **Screen: API keys.** All credentials for the org, in one place, clearly typed. Two groups: **Agent
> keys** (scopes read/write, project pin or global, last-used, expiry, revoke) and **Sync keys**
> (scope `sync`, pinned project, used by a linked deployment — cross-links to screen 7). Create panel
> with name, scope checkboxes, project scope (or global), expiry. New key shown once in a mono box +
> copy + a per-tool **connect** snippet. Emphasize scope/lifecycle: expiry, revoke as a kill switch,
> keys never out-rank their minter. States: no keys, mixed keys, an expired key (red "expired"), a
> revoked one.

### 17 · Security  *(P2)*
> **Screen: Security.** Org-level security settings. Sections: **SSO** (connect an IdP — SAML/OIDC
> fields, enforced-SSO toggle), **BYOK provider keys** (bring-your-own AI provider keys, stored
> write-only + encrypted at rest — show `key_set` state, never the key), **Data residency / privacy**
> (where graphs/memory live; a global "never accept vectors" note reinforcing the re-embed model), and
> **Audit log access**. States: nothing configured (defaults), SSO enforced, BYOK set.

### 18 · Plan & billing  *(P1)*
> **Screen: Billing.** Current plan card (tier name, price, renewal date), a **plan comparison** row
> (Free / Team / Business — seats, projects, MCP quota per tier, feature check rows), seat count vs
> included, payment method, invoices list. Primary action to upgrade/change plan. Tie limits to the
> real levers: seats, project cap, monthly MCP call quota, per-org burst cap. States: free plan
> (upgrade CTA prominent), paid plan, past-due (red banner).

### 19 · Usage & quotas  *(P1)*
> **Screen: Usage.** Consumption against plan. A big **MCP calls this month** meter (used vs monthly
> quota, projected end-of-month), the **per-org burst cap** (calls/min) with a recent spike chart, and
> a per-project breakdown table (calls, active agents, graph size). A note distinguishing the
> **monthly plan quota** from the **per-minute burst cap**. States: comfortable, approaching quota
> (amber), over burst (throttled — red rows).

### 6 · Project detail  *(P1)*
> **Screen: Project detail.** One project's home inside the org. Header (name, accent, description,
> sync status pill). Tabs or sections: **Overview** (items/memory/graph counts, last push), **Sync**
> (which deployment feeds it, key, freshness — a focused slice of screen 7), **Settings** (name,
> description, flags: share global memory, auto-extract lessons, expose MCP, **push code graph to
> cloud** opt-out), **Members** (the subset with access). States: healthy, never-synced, sync paused.

### 20 · Operator console home  *(P2)*
> **Screen: Operator console.** The **platform-admin** plane — visible only to operators, visually
> distinct from tenant UI (a subtly cooler/darker chrome + an "OPERATOR" mono badge so no one mistakes
> it for a tenant view). Home = platform health: total orgs, users, active deployments, aggregate MCP
> throughput, recent platform events. A left rail: Orgs, Users, Invites/Licensing, Audit. Note it's
> **hosted-only and operator-gated** (hidden entirely from tenants — a 404, not a 403). States:
> healthy platform, an org flagged over-quota.

### 21 · Operator — orgs list  *(P2)*
> **Screen: Operator / Orgs.** Every tenant on the platform. Table: org name, owner (mono handle),
> plan, seats used, projects, MCP usage vs quota, created, status (active / suspended). Row → an org
> detail drawer (members, usage, a **suspend / restore** and **impersonate for support** action,
> gated + audited). Search + filter by plan/status. Emphasize this is cross-tenant and every action is
> logged to the operator audit trail.

### 22 · Operator — users list  *(P2)*
> **Screen: Operator / Users.** Platform-wide users. Table: name + handle, email, org memberships
> (chips), last active, status. Actions: disable, force password reset, view the orgs they belong to.
> Search. States: populated, a disabled user (faint + red status).

### 23 · Operator — platform invites & licensing  *(P2)*
> **Screen: Operator / Licensing.** Operator-issued **platform invites** (for licensed/closed signup
> modes): mint an invite that lets someone create an org, choose the plan/entitlements it grants, set
> expiry. A table of issued invites (recipient, plan granted, status, revoke). A **signup mode** control
> for the platform (open / invite-only / licensed). Explain the modes inline. States: invite-only mode,
> a few outstanding invites, one redeemed.

### 24 · Operator — audit log  *(P2)*
> **Screen: Operator / Audit.** The append-only platform event ledger. A dense, filterable table:
> timestamp (mono), actor (operator/user/agent handle), action, target (org/project/key), and a
> details expander with the event meta JSON. Filters by actor type, action, org, date range. Emphasize
> immutability ("append-only") and that every operator action lands here. States: streaming recent,
> filtered, empty range.

### 1 · Accept invite / join a tenant  *(P1)*
> **Screen: Accept invite.** A centered, focused card (no full shell). Shows the org you're invited to
> (name, who invited you, the role you'll get), the account you're accepting as, and a single **Join
> [org]** primary action, plus a decline link. Handle expired/invalid invite as an error state (red
> card, "this invite has expired — ask [inviter] to resend"). States: valid, expired, already a member.

### 2 · Create org (first-run)  *(P1)*
> **Screen: Create org.** First-run tenant setup, centered flow. Steps (or one form): org name +
> accent, your role (owner, fixed), an optional "invite teammates" field, and a closing card that
> teases the next step — **link a deployment** (mint a sync key) — so the empty tenant has an obvious
> first action. Keep it short and confident. States: form, submitting, done → "link your first
> deployment" CTA.

### 3 · Sign in  *(P2)*
> **Screen: Sign in.** Centered auth card: email + password, a primary Sign in, an **SSO** button
> (shown when the org enforces it), forgot-password link, and a subtle brand mark. Match the dark
> system exactly. States: default, SSO-enforced (password hidden, only "Continue with SSO"), error
> (bad credentials).

### 13 · PRDs  *(P2)*
> **Screen: PRDs.** Product docs attached to the org/projects. A list of PRDs (title, status chip —
> draft/active/archived, owner, updated), a detail view rendering the PRD with section coverage (which
> sections have tracker items), and a "grill" affordance (the PRD-hardening flow) if it fits. States:
> no PRDs, a draft, an active PRD with coverage bars.

---

## Notes for whoever runs the set

- **Design P0 first** (4, 5, 7, 8, 9, 10) — that's the core "link a box → push a graph → work gets
  triaged" loop and it validates the whole tenant model. The rest hangs off it.
- **Reuse components across screens.** Once the status pill, the linked-instances table, and the stat
  card look right in one screen, tell the design tool to reuse those exact shapes in the next — that's
  how the set stays coherent.
- **Keep the local↔cloud seam honest.** Screens 7 and 8 are the hinge with the already-built local
  Sync/Link page; their vocabulary (deployment, sync key pinned to one project, re-embed, freshness)
  should match that page 1:1.
- These prompts describe *intent and data*, not pixels — let the design tool compose; correct it
  toward the tokens in Part 1 when it drifts.
