# PRD-21 — The cloud org plane: a hierarchy over the projects, a galaxy over the graphs

**Ledger id:** GRPH-P21
**Status:** **approved — re-earned 2026-08-20**, after the 2026-08-18 `scope-change` rebaseline cleared the dimension
verdicts and re-opened the grill. **The 2026-08-17 baseline still governs** until a new one is
earned; anything built meanwhile is measured against it. First baseline: three batches, 24
questions; what they settled is recorded in the section each decision governs, and the grill
turns themselves are stored on the PRD.
**Depends on:** PRD-20 (the graph engine — layout, galaxy, presence) · GRPH-74/75/76 (org layer,
plans + quotas, cross-tenant isolation) · GRPH-137/138/139 (link, MCP proxy, incremental push) ·
GRPH-219 (the hosted endpoint — in progress)
**Complemented by:** GRPH-82 (Stripe self-serve) · GRPH-185/186/187 (tracker integrations) ·
GRPH-191 (standup + triage board) · GRPH-222 (org-scoped agent key)
**Touches:** `web/src/App.tsx` · `web/src/components/shell/*` · `web/src/features/ProjectContext.tsx` ·
`web/src/lib/graph/*` · `backend/app/models/__init__.py` · `backend/app/routers/orgs.py` ·
`backend/app/services/code_sync.py` · `backend/app/services/quotas.py`

## 1. Overview

The hosted backend is finished and running. Orgs, roles, invites, plans, quotas, cross-tenant
isolation, sync ingest, the MCP endpoint and the operator API all shipped (GRPH-74/75/76/91/
92/93/94/95/137/139/218/220), and `cloud.graphban.dev` currently reports `hosted_mode: true`,
`signup_mode: invite_only`, embeddings live.

What is missing is not capability. It is **a place to stand.** The hosted product has no level
above a project, so an organization with seven repos is seven visits to the same single-project
app, and the one question an org exists to answer — *how do these repos relate?* — cannot be
asked, stored, or drawn.

### 1.1 The two claims this PRD makes

**The org is a level, not a nav item.** Today `/organization` is a sibling of `/tracker` in a
flat rail (§3.1) and the active project is ambient React state that is neither in the URL nor
persisted (§3.2). Both have to change before "drill down into a project" is a sentence that
means anything.

**Repos relate to each other, and nothing in the system can say so.** `CodeEdge` is
`UNIQUE(project_id, src, dst, type)` — both endpoints are paths inside one graph, so an edge
*cannot* cross a project (§3.4). The super galaxy is therefore not a view over existing data.
It is a new relation, and the whole of its difficulty is deciding what makes one true.

### 1.2 What this is not

- **Not a second app.** One SPA, one component set, one API client. The org plane wraps the
  project app; it does not fork it. A self-host build renders the project level and never sees
  the org rail — the same `hosted_mode` conditional that already gates `/organization`.
- **Not a re-theme.** The token set stands. `docs/design/cloud-tenant-design-prompts.md` Part 1
  remains the visual contract.
- **Not the integrations PRD.** Jira / Confluence / Linear / Trello are PRD-23. The screen is
  *designed* here so the rail composes once, and *hidden* until that PRD lands (D9).
- **Not the analytics / program-management PRD.** Standup, burndown, model metering and billing
  are PRD-24. Same treatment: designed, hidden, not built.
- **Not custom org domains.** `acme.graphban.dev` is a later thing. D1.2 keeps the seam open by
  refusing to hardcode `/org`; it builds nothing.
- **Not billing self-serve.** Plans stay operator-assigned until GRPH-82.
- **Not a new tracker, memory store, or PRD editor.** Those exist and are reused verbatim at the
  project level.

### 1.3 The load-bearing invariant

> **A cross-repo edge must name the file that proves it.**

Every edge in the super galaxy carries `evidence` — a concrete, re-checkable fact, at minimum a
path in a real repo (`web/package.json` declaring `@acme/core`). An edge whose evidence cannot
be re-verified on the next push goes **stale**, exactly as `CodeNode.fresh` already works. There
is no confidence-scored, embedding-derived edge in this graph.

This is not fastidiousness. A galaxy inferred from summary similarity would render beautifully,
be impossible to falsify, and be wrong — and it would be wrong in the specific way this codebase
has spent a release learning to detect: **plausible output that no test can refute.** An edge
that names a file can be checked by opening the file.

## 2. Goals

- **G1** — An org member sees every project at once: health, sync freshness, who is working.
- **G2** — A project is addressable. A URL identifies a project and a view, and survives a refresh.
- **G3** — The relationships *between* repos are stored, evidenced, and drawn.
- **G4** — From a project's code graph, the edges that leave the repo are visible as edges, not prose.
- **G5** — Teams exist, and a team grant is the unit of access administration.
- **G6** — A developer can see which of their agents are running, on which projects, from the org.
- **G7** — A PM can drive the grill and decompose loop at the project level without leaving the plane.
- **G8** — Every screen handed to Claude Design is backed by an endpoint that exists or is specified here.
- **G9** — Self-host loses nothing. Every addition is org-gated and absent when `hosted_mode` is off.

## 3. Problem (verified against the tree)

### 3.1 There is no hierarchy — the org is a sibling of Tracker

`web/src/components/shell/LeftNav.tsx:92–113` renders one flat list: Tracker, Requests,
Dashboard, Links, Code graph, Roadmap, MCP Tools, Fleet, Memory review, Activity, PRDs, then —
conditionally — Organization and Operator, then Feedback Kit and Settings. `App.tsx:54–76`
matches it: twenty sibling routes under one `AppFrame`, none of them nested.

So the org is *inside* the project app, one rung below where it belongs. There is no route, no
component, and no concept for "the org, across its projects."

### 3.2 A project is ambient state, not a place

Two facts from `web/src/features/ProjectContext.tsx`:

```tsx
const [activeId, setActiveId] = React.useState("");
const active = projects.find((p) => p.id === activeId) ?? projects[0] ?? null;
```

- **The project is not in the URL.** No route carries a project segment. `/code` means "the code
  graph of whichever project is currently selected." **You cannot send anyone a link to a
  project's anything** — not a graph, not a PRD, not a backlog.
- **The selection is not persisted.** Plain `useState`, no storage, no URL. A refresh drops you
  back to `projects[0]`.
- **A second copy of it lives in the API client.** `lib/api.ts:70` holds a module-level
  `activeProjectId` that `ProjectProvider` syncs in an effect, and thirteen call sites write
  through it. Harmless while only the switcher moves the project. A correctness problem the
  moment a route does — see D1.1, which is why this is P0 and not a detail of the route work.

For a single-project self-host this is invisible. For an org plane whose entire interaction model
is *drill in, then share what you found*, it is the foundation and it is absent.

### 3.3 The org has no aggregate, and that is deliberate

`authz.require_readable` — docstring verbatim: *"A None/omitted project_id fails closed (no
cross-tenant 'list everything')."* Every analytics route (`/api/dashboard`, `/api/roadmap`,
`/api/links`) passes through it, so a null project is a 404, not a rollup.

That guard is correct and stays. The consequence is that **an org-scoped aggregate does not
exist anywhere in the codebase and cannot be obtained by relaxing a filter.** `quotas.usage()`
is the sole org-scoped read — projects, seats, shards, calls this month. Four counters. The org
dashboard needs a genuinely new endpoint whose scope is the org and whose guard is membership.

### 3.4 An edge cannot cross a project

```python
class CodeEdge(Base):
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    src: Mapped[str] = mapped_column(String)  # path
    dst: Mapped[str] = mapped_column(String)  # path
    __table_args__ = (UniqueConstraint("project_id", "src", "dst", "type", name="uq_code_edge"),)
```

One `project_id` for the edge, and both endpoints are bare paths interpreted within it. `Link`
(item↔item) is scoped the same way. There is no representation of a relationship between two
projects at any layer of the system.

### 3.5 Membership is read-only over HTTP — but the service layer is not

The only membership endpoints in the tree are `GET /api/orgs/{id}/members` and
`GET /api/projects/{id}/members`. **No route changes a role, removes a member, or grants project
access**, so over HTTP a member arrives by accepting an invite and stays forever at the role the
invite carried.

**The capability, however, already exists and is wired to nothing.** `services/orgs.py` implements
`set_member_role` and `remove_member` in full — role validation, `require_org_admin` on the actor,
a refusal to grant a rank above your own, a refusal to act on yourself, and a removal that
cascades project memberships and reports what it dropped rather than returning a bare success.
Verified: **no router calls either function.**

So this is a missing *surface*, not a missing capability, and D8 is correspondingly smaller than
it first appeared. The gap still blocks D5 — teams cannot administer access through an API that
cannot write access — but closing it is wiring, not design.

### 3.5.1 The lockout the current rules create

`authz.require_org_admin` is the **only** org-administration gate and it accepts owner *or*
admin, so the two are already equivalent in power. What `owner` uniquely carries is three
*prohibitions*: its role cannot be changed, it cannot be removed, and it cannot be granted to
anyone else.

The consequence is a live hole rather than a design choice: **an org whose owner departs has no
path to a new owner.** Ownership is not transferable, the owner cannot be demoted, and no
endpoint exists to move it. D8 resolves this.

### 3.6 The design prompt set describes screens nothing backs

`docs/design/cloud-tenant-design-prompts.md` is otherwise the right artifact and its Part 1
stands. But seven of its 24 screens describe capability that does not exist. Audited against the
tree, 2026-08-17:

| Screen | What is missing |
| --- | --- |
| 10 · Triage & collision clustering, *cross-repo* | `collision.py` and `clustering.py` scope every query by `project_id`. Cross-repo collision does not exist. The prompt calls this "the cross-repo reason the cloud exists." |
| 14 · Members & roles — change role, remove | No write endpoint (§3.5). |
| 17 · Security — SSO/SAML/OIDC, data residency | No code of any kind. |
| 18 · Billing — payment method, invoices, upgrade | No Stripe. `Organization.stripe_customer_id` is an unused column; plans are set by an operator email allowlist. GRPH-82. |
| 19 · Usage — spike chart, end-of-month projection | `OrgUsage` holds one row per period. No time series exists to chart. |
| 21 · Operator — suspend / restore / impersonate | No endpoints. |
| 24 · Operator — audit log | `events.list_events` takes `project_ids`. No cross-tenant read. |

D9 disposes of each. The rule for this PRD: **Design is handed nothing from this table.**

## D1 — The hierarchy, and the URL that carries it

Two levels, both addressable.

```
/org                          org dashboard              (D2)
/org/galaxy                   the super galaxy           (D3)
/org/teams                    teams + grants             (D5)
/org/projects                 projects list
/org/deployments              linked instances           (D6)
/org/integrations             ── reserved, PRD-23        (D9)
/org/analytics                ── reserved, PRD-24        (D9)

/p/:tag                       project home
/p/:tag/code                  code graph (+ arrows out)  (D4)
/p/:tag/prds/:id              the PRD workspace          (D7)
/p/:tag/tracker  …            existing views, unchanged
```

**The project segment is `Project.tag`, not `id`.** Tags already exist, are unique per install,
and are already how item keys render (PRD-13). A URL built on tags is readable and shareable; one
built on `prj_a1b2c3` is neither.

**What a tag actually is**, because the first draft of this section got it wrong: `tagging.TAG_RE`
is `^[A-Z][A-Z0-9]{1,3}$` — **two to four characters, uppercase**. `GRPH`, `AL`, `SA`. So the URL
is `/p/GRPH/code`, never `/p/core/code`. The tag is **used verbatim** and matched
case-insensitively; there is no canonical-lowercase rewrite, because that would invent a second
representation of an identifier that already has exactly one. It also means the URL segment and
the item keys a reader already knows (`GRPH-406`) agree.

**No reserved-word list is needed, and none exists.** `services/projects.py:82` is explicit —
"no reserved-word list ships in the product". None is required here either, because the tag is
**positional**: a project tagged `CODE` gives `/p/CODE/code`, which is unambiguous. *(An earlier
draft of this PRD claimed a reserved-word guard existed. It does not.)*

Retag moves the URL. `ProjectTagHistory` already records the old tag, so an old tag resolves and
`<Navigate replace>`s to the current one **for a member**; for a non-member it 404s exactly as the
new tag would, so the redirect leaks nothing.

`ProjectContext` keeps its API and changes its source: the active project is **derived from the
route** at the project level, and the picker becomes navigation rather than state. At the org
level there is no active project — a fact the context must be able to represent, so `activeId`
becomes legitimately empty rather than silently `projects[0]`.

Measured, so the cost is not guessed: 20 routes; 20 `to="/…"` occurrences of which **14 are one
array** in `LeftNav.tsx`; 3 `<Link>` and 9 `navigate()` calls across six files; **2 of 22**
frontend tests mount a router. Of the 20 files consuming `useProjectCtx`, **15 destructure only
`activeId`** and hand it to a query hook — changing where that id comes from does not touch
them. Two call `setActiveId`; they become `navigate()`.

### D1.1 — The ambient project id is the actual risk

`lib/api.ts:70` holds a module-level `activeProjectId`, and `ProjectProvider` syncs it **in a
`useEffect`**. Sixteen call sites read it. Five put it straight into a request body —
`createItem`, `addShard`, memory search, `createPrd` — and `projectQuery()` appends it to eight
platform routes, including `github/connect` and `gdrive/connect`.

This is safe **today**, and the comment above the variable shows it was reasoned about: the only
thing that moves the project is the switcher, so a render always separates the change from the
user's next action.

**Putting the project in the URL destroys that guarantee.** The route changes synchronously on a
deep link and on back/forward; the effect fires one render later. A query issued in that window,
or a fast click, targets the previous project. `github/connect` is the worst case — it wires an
integration into the wrong project's `PlatformConfig` — and in hosted mode, for a user who
belongs to two orgs, the backend has nothing to reject: they have access to both.

So D1 **deletes** the ambient variable rather than re-pointing it:

- **The project is passed explicitly** at all thirteen sites (five writes, eight platform
  routes). `createItem` becomes uncallable without one, so a future call site cannot forget.
- **The route is the only source.** Nothing caches it and no effect mediates it — the same
  one-source-of-truth rule the service layer already lives under.
- **A mismatch is loud.** The request's project and the route's project must agree; a
  disagreement throws in development rather than silently writing a row.

This is the work in D1. The route shapes above are a morning. This is the part that can corrupt
data if it is done by re-pointing the effect.

**Settled in the grill.** Two rules follow from the same reasoning and belong at the server
boundary, not only in the client:

- **A route-scoped endpoint refuses a project in its body.** 400, never coerce. Silent coercion
  is the wrong-project write this decision exists to delete, relocated one layer down.
- **A tag is a lookup, never a grant.** The route resolves tag → project id and every existing
  guard then runs unchanged on the resolved id. A tag from another org resolves to a project the
  caller cannot read and returns **404**, indistinguishable from nonexistent — so tags being
  short and guessable is not a weakness. `require_readable` already documents the rationale;
  this applies it, and adds no policy.

### D1.2 — Additive, not a cutover

The tag routes mount **alongside** the existing flat ones, both rendering the same views.
Nothing is deleted in this PRD, so there is no moment where the app is half-migrated: the old
paths keep working and can be removed whenever it suits.

**They are client-side redirects, not 301s.** nginx serves a static bundle behind a catch-all, so
the server cannot know what `/code` means, and last-used lives in `localStorage` — client-side
only. A flat path mounts a resolver that reads last-used, falls back to the first readable
project, and issues `<Navigate replace>` to `/p/:tag/<view>`. `replace` so back does not
ping-pong. When nothing resolves: `/org` in hosted mode, the create-first-project flow on
self-host.

**Nothing hardcodes `/org`.** Custom per-org domains (`acme.graphban.dev`) are not built here,
but D1 must not foreclose them. Every org-plane path is built from a single **org base** that is
`/org` today and can become `/` when an org is served from its own host — one helper, all links
through it, and no test asserting a literal `/org/…` beyond the one asserting the base. Honoring
this now costs approximately nothing; skipping it costs every org link later.

Self-host: `hosted_mode: false` mounts the project routes at the root exactly as today and never
registers `/org/*`. No self-host URL changes. On boot, when config reports `hosted_mode: false`,
any org keys in `localStorage` are cleared — a self-host build must not resurrect an org context
it has no way to serve.

**Last-used** moves to `localStorage` — the fix for §3.2's second half, and what lets the flat
redirects resolve anywhere sensible.

### D1.3 — The third namespace: a reporting endpoint per project

The root is now contested between three things, and D1 has to say so: `/org/*` (the plane),
`/p/:tag/*` (a project), and **public per-project reporting paths**, which sit deliberately
outside both so they survive an org moving to its own domain.

This is mostly already built and the PRD should say so rather than invent a mechanism.
`PlatformConfig.share_token` exists today — unique, unguessable, and in hosted mode the **only**
accepted way to address a project publicly. The model comment is explicit that this closes the
"name any project_id unauthenticated → cross-tenant read" hole. What changes:

- **The URL becomes a root-level path**, `/r/<share_token>`, rather than
  `/embed/feedback?token=…`. Root-level and outside `/org` and `/p/:tag`, so it is servable from
  any host. The existing query form keeps working — additive, per D1.2.
- **Every project gets a token at creation.** Today `share_token` is minted only when sharing is
  switched on. Identity should always exist; **exposure stays opt-in** — `public_share_enabled`
  remains the separate switch and a project that has not opted in still 404s exactly as now.
  *A token that exists is not a project that is published.*
- The token in a path rather than a query is the same secret either way; it is a share token
  designed to be handed out.

## D2 — The org dashboard: the first aggregate

A new read, `GET /api/orgs/{org_id}/overview`, guarded by org membership. It returns, per
project the caller can read: item counts by status, open claims and their holders, code-graph
node count, last sync push, and freshness. Plus org totals and the four `quotas.usage()`
counters against plan limits.

**It is a new endpoint, not a relaxation of `require_readable`.** §3.3's guard stays exactly as
written; this route resolves the project list from org membership first and then reads scoped,
so "list everything" is never reachable even as an internal call path.

Composition rule, inherited from PRD-20 D4: this is a **join, not a new write path**. Every
number it returns already exists in a table. If a figure on this screen has no query behind it,
it does not go on the screen.

Empty state matters more than the populated one. A brand-new org has no projects and no linked
deployments, and the screen's job is then exactly one thing: point at minting a sync credential.

## D3 — The super galaxy: an edge between repos must name the file that proves it

The flagship, and the only genuinely new data in this PRD.

**Nodes are projects.** The sync model already makes this 1:1 — a sync credential pins to one
project, and one local deployment pushes one project's graph (`routers/sync.py`, verified). A
repo *is* a project on the cloud side.

**Edges are `ProjectEdge` rows**, org-scoped, typed, and evidenced:

| Type | Provenance | How it is obtained |
| --- | --- | --- |
| `depends_on` | **Manifest** | The local pusher parses `package.json` / `pyproject.toml` / `go.mod` / `Cargo.toml` and declares the internal dependency names it finds. |
| `serves` | **Declared contract** | A project declares an API it serves; another declares it consumes it. Config, not inference. |
| `declared` | **Human** | A PM draws it and must supply a `reason`. |

Resolution runs through a name registry: `Project.provides` — the package names a project
publishes (`@acme/core`, `acme-core`). A manifest dependency that resolves to a sibling project
becomes an edge; one that does not is an external package (npm, PyPI) and is **dropped**, not
drawn. The galaxy is repos in this org, not a dependency tree of the world.

**What is deliberately excluded: inference.** No edge is created from embedding similarity,
shared vocabulary, or summary overlap. §1.3 is the reason. Two repos that both describe
"authentication" are not related; two repos where one's lockfile names the other are.

**Staleness, not deletion.** Each push re-declares that project's manifest dependencies. An edge
whose evidence is no longer declared is marked `fresh = False` and renders faint — the same
handle `CodeNode` already uses, and for the same reason: a relationship that has quietly
disappeared is information, and deleting it silently is the absence-reads-as-clean failure.

**Rendering reuses PRD-20 wholesale.** `computeLayout` in the worker, `componentsOf` for
clusters of related repos, hulls for the clusters, `useGraphViewport` / `useGraphFind` /
`useGraphPins` unchanged. A galaxy of projects is a graph with tens of nodes, so the 800-node
budget and `iterationsFor` are untroubled. Presence composes: PRD-20 already colours a node by
who holds it, and at this level the same treatment says *which repos have agents in them right
now*. That is the org view of the fleet, for free.

**Node weight** is code-graph node count; **edge weight** is the number of **currently-fresh**
evidence entries — a repo that dropped a dependency must not keep a fat edge. Both are counts of
real rows.

### Settled in the grill

- **A project is one pushed graph = one sync credential = one checkout.** A monorepo is ONE
  project that `provides` many names; package-to-package relationships inside it are internal and
  belong in its code graph. No self-edges. **The galaxy's resolution is the checkout, not the
  package** — a stated limit, not a defect.
- **Two projects claiming the same name draw nothing**, and the collision is surfaced on the
  projects screen. An ambiguous name is a coin flip, and §1.3 forbids guessing.
- **The cloud never validates that a file exists.** It has no checkout. It enforces only that
  `evidence` is non-empty (422); path existence is a pusher-side invariant. A rename arrives as a
  new entry and the old one ages out — no special handling.
- **The client sends facts, not edges.** It cannot know what other projects exist in the org, so
  it pushes a `manifests` block and the server resolves each name against `Project.provides`
  within the pushing key's org.
- **An omitted `manifests` block and an empty one mean different things.** Omitted → an older
  client that did not look → **stales nothing**. Present-but-empty → looked, found none →
  **stales the edges**. Collapsing them writes absence-reads-as-clean permanently into a wire
  format, where it is far harder to dig out than a bad test.
- **Unresolved names are dropped per-edge but counted in aggregate** — the push response reports
  "12 dependencies resolved to external packages". A silent drop with no count is the failure
  mode this codebase keeps finding.
- **Stale edges are never auto-purged**, and their evidence is never trimmed. A purge-after-N
  policy rebuilds absence-reads-as-clean on a timer and contradicts the no-sweepers rule the
  lease clock follows; a stale edge with its evidence trimmed is worse than a deleted one,
  because it is a relationship with no explanation. Purging is an explicit operator action.
- **The empty galaxy has three distinguishable states**: no projects; projects but nothing pushed
  (draw the nodes — the org is not empty, the *edges* are); projects pushed with no internal
  dependencies (a legitimate answer that must not look like a failure).

## D4 — The arrow out

Inside a project's code graph, a dependency that leaves the repo renders as an edge to a
**project stub node** — a distinct shape carrying the sibling project's name — and clicking it
navigates to `/org/galaxy` focused on that edge, or to `/p/:tag/code` for the target.

**No change to `code_edges`.** The arrow is rendered from `ProjectEdge.evidence`, which already
names the file that carries the dependency. So the edge in the UI is anchored on the real node
`web/package.json`, and its tooltip is the evidence string. No migration on a hot table, and
every arrow is explainable by opening one file.

This is G4 and it is the payoff for §1.3 being strict: because an edge had to name a file, the
project-level view gets to draw it in the right place without any extra data.

## D5 — Teams, and what a grant writes

**Built.**

A `Team` belongs to an org, has members (users) and grants (project + access level).

**A grant materializes.** Creating or changing a team grant writes `Membership` rows —
it does not add a resolution step to `can_read` / `can_write`. Those two functions are the
hottest authorization path in the application and every route depends on them; making them
resolve team closure at read time changes the risk profile of the entire app for a feature that
is fundamentally administrative.

Materializing means the blast radius is one sync function with a test suite, and every existing
authz test keeps its meaning. It costs one thing — direct `Membership` edits could drift from
their team — which D8 handles by making team-derived memberships identifiable and refusing
direct edits on them.

Teams are additive to the two tiers that exist: `OrgMembership` (owner > admin > member) still
governs the org; `Membership` still governs a project; a team is the instrument that writes the
second in bulk.

**Settled in the grill.** **Authorization never reads `origin` — it reads `access`.** `origin`
exists for D8's refusal-to-edit and for recompute, and touches no permission decision; that bound
is what keeps this feature administrative. Revocation therefore **recomputes** a user's derived
memberships from the grants that remain, in the same transaction — it does not delete rows tagged
with the revoked team, so a second team still granting the project keeps the row. Direct
memberships always survive; where direct and derived collide, direct wins and the grant
materializes nothing. Access resolves to the highest across all sources.

## D6 — Linked deployments, and why the drill-down is mostly not an iframe

**Built.**

The vision asked for a developer to zoom into their own local deployment and see which agents
are running on which projects. **In a linked topology the cloud already knows this**, and the
discovery is worth stating plainly because it removes the hardest engineering risk in the PRD.

`services/mcp_proxy.py`, verbatim: when a local instance is linked, agents talk to the local
`/api/mcp`, *"but only the code-graph tools run locally… Everything else (items, claims, memory,
PRDs, backlog…) is forwarded to the cloud, which stays authoritative for mutable, contended
state."* `LOCAL_TOOLS` is seven names. `claim_next`, `claim_cluster`, `heartbeat`, `next_cluster`
are not among them.

So every claim, lease, heartbeat and enrolment from a linked box **is already a cloud row**. The
`Agent`, `Enrolment` and `AreaReservation` tables the fleet view reads are populated on the
cloud by forwarded calls. "Which agents are running, on what" is a query, not an embed.

**Linked deployments** (`/org/deployments`) therefore renders from data the cloud holds: the
sync credential, its pinned project, the last `sync_code_graph` event and its node counts,
freshness, and the agents currently active against that project. One honest gap — **the cloud
stores no deployment identity**; there is no hostname or label anywhere in the ingest path. The
sync key's name stands in for it, which makes naming the key at mint time load-bearing, and D6
says so on the mint screen.

**Reaching the box is a link, not an embed** *(rebaselined 2026-08-18)*. The console shows the
deployment's address and opens it in a new tab. Nothing is framed, proxied, relayed or tunnelled.

This deletes a problem class rather than managing one. Every difficulty an embed carries —
mixed content, `frame-ancestors`, two sessions, and browsers steadily tightening public→private
network access — exists because framing is a *subresource*. **Navigating a new tab to an http
address from an https page is permitted.** None of it applies, and nothing is given up: the
screen was already required to be complete without the embed, and every valuable signal on it is
cloud-held.

**Relay and reverse tunnel are rejected, not deferred.** Both invert the one-directional trust
model the product rests on — the box pushes, the cloud never reaches in — which is the substance
behind the privacy line on the sync screen. Both are real infrastructure (connection state, auth,
backpressure, multi-tenancy, a new outage class) bought for a small residual, since the MCP proxy
already puts items, claims, leases, agents, memory and PRDs in the cloud. The standing rule:
**when something local turns out to be worth seeing in the cloud, push it up as payload; do not
build a path down.** A cloud→box channel must justify itself against a capability that genuinely
cannot be delivered by sending more data upward.

One thing is added rather than removed. The cloud stores nothing pointing back at a deployment —
`SyncLink.cloud_url` runs the other way — so the push carries a **self-reported base URL**. Two
properties govern how it is shown:

- **It is a hint, never a guarantee.** The same box answers at different addresses from different
  networks; `http://ubuntu-srv:8080` is right on that LAN and meaningless from anywhere else. A
  per-user override lives in `localStorage`.
- **Render the address as text, then link it.** The console cannot test reachability — that is
  the viewer's network, and a cross-origin probe would be blocked or hang. So a button reading
  "Open deployment" that dead-ends into a connection error is worse than no button, while a
  visible `http://ubuntu-srv:8080` tells someone on the wrong network everything before they
  click.

## D7 — The project plane

**Built.** Most of it landed with D1: `/p/:tag/*` mounts the existing views unchanged and
`/p/:tag/prds/:id` gives the PRD workspace its URL. Triage arrived correctly labelled
per-project. What D7 added on top is the landing pad at `/p/:tag` — counts, routes into the
project's own surfaces, and the dependency strip read from the galaxy in **both** directions.

`/p/:tag` is where the existing app lives, reused unchanged: tracker, requests, memory, roadmap,
activity, fleet, code graph.

The one addition is emphasis. **The PRD workspace is already built and is the cheapest win in
the vision** — `GrillPanel`, `GrillProgress`, `IntentDiff`, `AcceptancePanel`, `PrdEditorView`,
plus `decompose_prd` and `prd_coverage` on the backend. A PM driving the grill-to-decompose loop
needs no new capability, only to be given a place in the hierarchy and a URL. `/p/:tag/prds/:id`
is that.

Triage and bug views at this level are the **existing per-project** clustering (`clustering.py`,
`collision.py`), correctly labelled as per-project. Cross-repo triage is §3.6's first row and is
not designed here (D9).

## D8 — Membership mutations

**Mostly wiring.** §3.5 establishes that `set_member_role` and `remove_member` already exist and
are reachable from nothing. Three routes expose them:

- `PATCH /api/orgs/{org_id}/members/{user_id}` — change org role.
- `DELETE /api/orgs/{org_id}/members/{user_id}` — remove from the org, cascading project
  memberships and reporting what was dropped.
- `PUT /api/projects/{project_id}/members/{user_id}` — grant or change project access
  (`write` / `read` / `none`). This one is genuinely new; the other two are not.

Every route records an `Event`. These are authority actions and `test_authority_gates.py` exists
to assert that authority actions stay human-adjudicated and audited.

### D8.1 — One floor invariant replaces three special cases

Owner-immutability protects a proxy for the thing that matters. What matters is that **somebody
can always administer the org**; what the rules currently enforce is that *one specific person
exists*, which is why a departing owner strands the org (§3.5.1).

Settled: **admin is equivalent to owner, and an org must always retain at least one
owner-or-admin.** That single invariant replaces the owner's three prohibitions, and it is
checked **transactionally** — under a row lock over the org's memberships, because the existing
self-action rules make zero admins unreachable sequentially but *not* under a race. `you cannot
change your own role` means A can demote B while A remains; two admins demoting each other
simultaneously is the only path to zero, and a lock is what closes it.

With the floor in place the owner becomes demotable and removable **provided another
administrator remains**, and the departing-owner hole closes without an ownership-transfer
endpoint.

### D8.2 — Provenance separates from power

Ownership is currently recorded *only* as the owner's `OrgMembership` row — `Organization` has no
`created_by`, and the operator console derives `owner_email` from that row (`routers/admin.py`).
Once the role is demotable that derivation breaks, so **`Organization.created_by` is added**.

The split is the point: **who created the org is a durable fact; who administers it is a mutable
role.** Conflating them is what made the role immutable in the first place.

Team-derived memberships carry their origin and refuse direct edit with a message pointing at
the team — the drift D5 accepted, made visible rather than silent.

## D9 — Reserved slots, and what Design must not draw

**Designed, then hidden until ready.** `/org/integrations` and `/org/analytics` are drawn in the
design so Claude Design composes the full rail once and the layout never needs re-cutting. The
shipped build renders **neither the nav item nor the route** until the backing PRD lands.

This reverses the first draft, which specified disabled entries carrying a "coming in PRD-23 /
PRD-24" note. §7's own risk is the reason: the danger is the plane reading as chrome, and two
permanently-greyed items in a six-item rail make that worse rather than better. Design once,
ship no promises.

**Withheld from Design entirely** — every row of §3.6. Each becomes a tracked item with the
missing capability named, so the gap is in the backlog rather than in a mockup:

| Screen | Disposition |
| --- | --- |
| Cross-repo triage | New item — requires an org-scoped collision service. Not yet filed, so it has no number: the ledger issues those and 22 went to the fleet supervisor PRD on 2026-08-20. Naming a future one here is what put two documents on 22 (GRPH-425). |
| Security / SSO | New item. Nothing exists; not designed. |
| Billing payment half | GRPH-82. The *display* half (plan, limits, usage) is in scope — it is backed by `/api/orgs/{id}/billing` today. |
| Usage charts | New item — requires time-series `OrgUsage`. The current-period meter is in scope. |
| Operator suspend / impersonate | New item. Operator list/detail screens stay read-only. |
| Operator audit log | New item — requires a cross-tenant events read. |
| Member mutations | **Not deferred** — built here as D8. |

## 4. Data model

New:

```python
class Team(Base):                 # org_id, name, slug, description, created_at
class TeamMember(Base):           # team_id, user_id
class TeamGrant(Base):            # team_id, project_id, access  (write|read)
class ProjectEdge(Base):
    org_id, src_project_id, dst_project_id
    type          # depends_on | serves | declared
    evidence      # JSON list; each entry names a repo path + the fact found there
    fresh         # False when the last push no longer declares it
    created_at, updated_at
    UniqueConstraint(org_id, src_project_id, dst_project_id, type)
```

Changed:

```python
class LinkedDeployment(Base):
    # Identified BY THE SYNC CREDENTIAL — the key a user pastes into `graphban link` is the
    # deployment's identity. The cloud authenticates keys, not machines, and a separate
    # deployment_id would imply a verification it cannot perform.
    api_key_id      # primary identity; the key's pinned project comes with it
    org_id, project_id
    label           # the key's name, overridable — display only
    hostname        # optional, self-reported, display only
    first_seen_at, last_push_at, last_push_nodes, retired_at
```

Evidence entries (`ProjectEdge.evidence[]`) — required `kind` (`manifest` | `contract` |
`declared`), `file` (repo-relative), `name` (the dependency found there); optional `version`,
`line`; plus `last_seen_push`, which is what ages an entry out. **Edge weight counts only
currently-fresh entries** — otherwise a repo that dropped a dependency keeps a fat edge forever.

Changed:

- `Project.provides` — JSON list of package names this project publishes. The registry D3
  resolves manifest dependencies against. Declared, never inferred.
- `Membership.origin` — `direct` | `team:<id>`, so D8 can refuse edits to derived rows.
  **Authorization never reads it** — it reads `access`. `origin` exists for D8's refusal and for
  recompute, and touches no permission decision.
- `PlatformConfig.share_token` — already exists; now minted at project creation rather than when
  sharing is switched on. `public_share_enabled` remains the separate opt-in (D1.3).
- `CodeSyncState.push_seq` — a monotonic per-project counter; what `last_seen_push` records.
- Sync push payload gains a `manifests` block — the dependency names the local box found and the
  file each came from. The client sends **facts, not edges**: it has no knowledge of what other
  projects exist in the org, so the server does the resolving. Additive and optional, and
  **omitted ≠ empty** (D3).

Unchanged, deliberately: `CodeNode`, `CodeEdge`, `Link`, `Item`, `Agent`, `AreaReservation`,
`OrgMembership`. **No migration touches `code_edges`** (D4).

## 5. Acceptance criteria

Mechanical, in the PRD-20 sense — each is a command or a click with one right answer.

1. **No module-level project id exists.** `grep -n activeProjectId web/src/lib/api.ts` returns
   nothing, and `createItem` does not typecheck without an explicit project.
2. Deep-linking straight to `/p/other/tracker` and immediately creating an item writes it to
   `other` — asserted against the **request body**, not the rendered list. The same test run
   against the pre-PRD code must fail, or it is not testing anything (§D1.1).
3. Navigating back from project B to project A and connecting GitHub writes to A's
   `PlatformConfig`. Verified against the request, for the same reason.
4. `/p/GRPH/code` loads that project's code graph in a fresh browser with no prior session, and
   `/p/grph/code` resolves to the same place. An invalid or foreign tag renders not-found without
   redirecting.
5. A refresh on any project URL returns the same project. Closing and reopening the browser
   returns the last-used project, not `projects[0]`.
6. Every pre-existing flat path still resolves, and lands on the tag form of the same view.
7. `GET /api/orgs/{id}/overview` returns every project the caller reads and **no project from
   another org**. A member of org A requesting org B's overview gets 404, not 403.
8. `require_readable(db, user, None)` still fails closed. The existing test asserting it is
   unmodified and green.
9. Every `ProjectEdge` has non-empty `evidence` — enforced at push time with a 422. Path
   *existence* is a **pusher-side** invariant, not a server validation: the cloud has no checkout
   and cannot know. The pusher read the manifest, so the file exists by construction.
9a. An **omitted** `manifests` block stales nothing; a **present but empty** one stales the
    edges. A test asserts both directions — collapsing them writes absence-reads-as-clean into
    the wire format.
9b. Two projects claiming the same name in `provides` produce **no edge**, and the collision is
    reported on the projects screen.
9c. A tag belonging to another org returns 404 from `/p/:tag`, indistinguishable from
    nonexistent. A route-scoped endpoint carrying a project in its body returns 400.
9e. `memberships` carries `UniqueConstraint(user_id, project_id)`, and a concurrent pair of team
    grant updates cannot produce two rows for one user and project. The test races them.
9d. Every project has a `share_token` at creation, and a project that has not opted into public
    sharing still 404s on `/r/<token>`.
10. Removing a dependency from a manifest and re-pushing marks the edge `fresh = False`. It is
    still visible, faint. Nothing is deleted.
11. A dependency naming a package no `Project.provides` claims produces **no edge**.
12. The galaxy renders through `web/src/lib/graph/layout.worker.ts`. No second layout
    implementation exists in the tree — `test_docs_sync` style grep, one owner (PRD-20 D1).
13. A team grant creates `Membership` rows with `origin = team:<id>`. Revoking the grant removes
    exactly those rows and no directly-created one.
14. `can_read` and `can_write` are byte-identical to their pre-PRD versions.
15. A role change or removal that would leave an org with **zero** owner-or-admin members is
    refused, including when two administrators demote each other concurrently — the test races
    them and asserts one survives. An owner IS demotable while another administrator remains.
    `Organization.created_by` still names the creator after that demotion. Every membership
    mutation writes an `Event`.
16. `/org/deployments` renders fully for a deployment that has reported no base URL, and for one
    whose address is unreachable from the viewer's network. No pane is empty or errored, and the
    address is legible as text before it is clicked. Nothing on the screen frames, proxies or
    probes the local instance.
17. With `hosted_mode: false`, no `/org/*` route is registered and the self-host nav is
    unchanged. The existing self-host acceptance walk passes untouched.
18. `test_cross_tenant.py` gains cases for the overview, galaxy, and team endpoints, and is green
    on **both** SQLite and Postgres/pgvector.

## 6. Phasing

- **P0 — the write path.** D1.1 alone: delete the ambient `activeProjectId`, pass the project
  explicitly at thirteen sites, add the mismatch guard. **Independent of the rest of this PRD and
  worth shipping on its own** — it closes a wrong-project write that is latent today, and it is
  the precondition for a route ever being allowed to move the project.
- **P1a — addressable projects.** D1.2's additive tag routes plus `localStorage` last-used. Small
  once P0 has landed, and reversible because nothing is deleted.
- **P1b — membership mutations.** D8. Backend-only, shares no file with the URL work, and
  independently shippable — it can run in parallel on a separate agent.
- **P2 — the plane.** D2 org dashboard, projects list, D6 linked deployments, then D5 teams
  (which needs P1b).
- **P3 — the galaxy.** D3 model + push payload + registry, then D4's arrow out.
- **P4 — polish.** Empty states, the deployment address and its override, reserved slots.

P0 and P1b have no dependency on each other and neither blocks the design pass. Design can start
at P2 as soon as D1's URL *shape* is agreed — it does not wait for any of this to ship. The
galaxy screens need D3's edge semantics fixed first, which is the reason they are in this PRD
rather than a later one.

## 7. Risks

- **The name registry is hand-maintained.** `Project.provides` is declared, never inferred —
  deriving it from directory names would be guessing. Unresolved names are dropped (counted and
  reported, not silent), ambiguous ones draw nothing, and a project that publishes nothing has no
  outbound `depends_on` edges: an honest empty rather than a wrong graph.
- **The galaxy's resolution is the checkout, not the package** (settled in §8). A monorepo is one
  project that `provides` many names, so package-level relationships *inside* it are invisible at
  this level. That is a stated limit, not a defect — but it is the thing most likely to be
  mistaken for one.
- **Custom org URLs are not built.** D1.2 keeps the seam open through the org base; nothing
  validates that the seam actually holds until someone tries it.
- **Materialized grants can drift** (D5). Mitigated by `Membership.origin` and D8's refusal, but
  a direct database edit still bypasses it. Accepted.
- **The ambient project id, until P0 lands.** The wrong-project write in D1.1 is latent *today* —
  narrow, because only the switcher moves the project, but real. It becomes reachable the moment
  a route can move the project, which is why P0 is ordered before P1a instead of bundled into it.
  The route work itself was measured and is not the risk: one nav array, six files, two tests.
- **The address is a hint that will sometimes be wrong** (D6). Mitigated by rendering it rather
  than hiding it, and by a per-user override — but a link to a box on a network you are not on
  will still fail, and the design accepts that in exchange for not building a tunnel.
- **Scope.** This PRD is the frame and the galaxy. Integrations, analytics, standup, cross-repo
  triage and billing are all real parts of the vision and all deferred. The risk is that the
  first hosted release looks like a shell around a single-project app — which is why D2's
  aggregate and D3's galaxy are the two things that must be genuinely good.
