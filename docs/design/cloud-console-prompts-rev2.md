# Graphban Cloud Console — design prompt set, revision 2

Supersedes the operator screens in `prd-21-cloud-console-prompts.md` and adds four screens that
set does not contain. **Part 1 of the original set still stands** — the visual system is
unchanged and this is not a re-theme. What changes is the information architecture (a
workspace/admin split), plus three genuinely new surfaces: roles and permissions, org branding,
and a custom domain.

**Workflow, unchanged:** paste the original **Part 1** verbatim at the top, then **Part 0** below
it, then **one** screen prompt from **Part 3**. One `*.dc.html` per screen.

---

## Part 0 — Read this before drawing

The last set produced a beautiful operator console that specified six things nothing backs, and
contradicted its own preamble twice. Not a criticism of the output — a gap in the input. This
part closes it.

Every screen below carries a tier. **The tier changes what you are allowed to draw.**

| Tier | Meaning | What to draw |
| --- | --- | --- |
| **BACKED** | An endpoint exists today. | Draw it fully. It can be wired the week it is approved. |
| **HALF** | Part of the screen has an endpoint; part does not. | Draw only the backed half at full fidelity. Render the other half as the *absence*, named. |
| **UNBACKED** | Nothing exists. | Draw it **only** with a mono `SPECULATIVE — NOT BACKED` chip in the page header **and** the named blocker in the chip's tooltip text. A mockup without that chip becomes a spec. |

### The audit, against the tree

| Screen | Tier | What backs it / what is missing |
| --- | --- | --- |
| Admin shell + nav | **BACKED** | Pure IA. Routing only. |
| Users & access — list | **BACKED** | `GET /api/orgs/{id}/members`, `GET/POST/DELETE /api/orgs/{id}/invites`. |
| Users & access — change role, remove | **UNBACKED** | **No write endpoint exists.** PRD-21 §3.5. Specified as D8; not built. This blocks the roles work below. |
| Roles & permissions tab | **UNBACKED** | Roles are hardcoded strings, not rows. See "the three role systems" below. |
| Branding — logo | **UNBACKED** | `Organization` has no logo column. Byte storage precedent exists: `Attachment` (bytes in DB, served public-read by unguessable id). |
| Branding — palette | **UNBACKED** | `Project.accent` exists; `Organization` has no colour of any kind. |
| Custom domain | **UNBACKED** | Nothing app-side, and the app is the small half — see the prompt. |
| Integrations | **HALF** | GitHub and Google Drive connect/disconnect exist — but `PlatformConfig` is **one row per project**, not per org. |
| Billing | **HALF** | `GET /api/orgs/{id}/billing` returns plan + limits + usage. No Stripe, no invoices, no self-serve upgrade. |
| Operator console | **BACKED** | Shipped. Corrections in §H. |

### Corrections to Part 1 that the last output needed

Part 1 stands, but the generated console contradicted it. State these explicitly:

1. **Org roles are `owner | admin | member`.** Only those three. The last output invented
   `PRODUCT`, `DEVELOPER` and `ANALYST` and coloured all five — they exist nowhere in the
   codebase, and Part 1's own preamble already said "owner > admin > member". If revision 2's
   roles work lands, this changes; until then, three.
2. **There is no "last active".** Nothing records a read. The event ledger records writes, so the
   honest column is **last write**, and its null is `no writes` — a distinct state from idle, not
   an em-dash.
3. **Invite expiry is one global setting**, not a per-invite choice. `INVITE_EXPIRY_DAYS` applies
   to every invite the deployment issues. Report the policy; do not draw a picker.
4. **Organizations have no accent colour** (projects do). Until branding lands, an org's dot is
   derived from its id, not chosen.
5. **Enterprise limits are finite** — 500 projects / 1,000 seats / 1M shards / 10M calls. Never
   render `∞`; a cap that exists must read as a number.
6. **Nothing is ever purged.** The last output put a `PURGE` action on expired invites while its
   own footer said "kept, not purged". There is no purge endpoint and there should not be one.
7. **A seat is a membership OR a pending invite.** `quotas.seat_count` counts both, so a seat
   total legitimately runs ahead of a member list. Any screen showing both must reconcile them
   out loud.

---

## Part 2 — The information architecture change

Today the left rail is one flat list of eleven workspace views with Organization, Operator,
Feedback Kit and Settings pinned to the bottom. Administering the org is scattered across it.

**The split: what you use, and what you administer.**

```
WORKSPACE                          ← doing the work, per project
  Tracker · Requests · Dashboard · Links · Code graph · Roadmap
  MCP Tools · Fleet · Memory review · Activity · PRDs

ADMIN                              ← running the org, org-scoped   ◄── new group
  /admin/users          Users & access      (+ Roles tab)
  /admin/branding       Branding & theme
  /admin/integrations   Integrations
  /admin/billing        Billing & usage

OPERATOR                           ← cross-tenant, platform admins only
  /operator             Platform · Orgs · Users · Licensing
```

**Three naming rules, and they are not cosmetic.**

- The org-admin **Users** screen and the operator **Users** screen are different populations —
  one org's members versus every account on the deployment. Same word, two planes. The admin one
  is titled **"Users & access"** and the operator one stays **"Users"** under the `OPERATOR`
  badge. Never let them render alike.
- **The operator console must move off `/admin`.** It is on `/admin` today, and this change puts
  the org-admin section there. The operator plane becomes `/operator/*` — which is also what the
  last design set already labelled it.
- Admin is a **section, not a page**. Its four screens share a sub-rail and a header; drilling
  into one never leaves the section.

**Who sees it.** The Admin group renders for `owner` and `admin` only — `authz.require_org_admin`
is the existing gate. A `member` does not see the group at all. Draw that state: the rail with no
Admin group is a real state, not an error.

---

## Part 3 — Screen prompts

### A · Admin section shell *(BACKED)*

> **Screen: Admin shell.** The org-administration section at `/admin`, drawn as the frame its
> four screens live in. A page header carrying the org name, its plan chip, and a mono `ADMIN`
> eyebrow; below it a horizontal sub-nav of four items — **Users & access · Branding ·
> Integrations · Billing** — with the active one marked by a lime underline and the others muted.
> The tenant visual system, not the operator one: this is warm/lime chrome, because an org admin
> is a tenant.
> A one-line orientation under the header: "Everything here applies to this organization. Project
> settings live on the project."
> **States:** viewed as `owner` (all four items); viewed as `admin` (all four); viewed as
> `member` — **the section does not exist**, so draw the rail without the group rather than a
> greyed one or a permission error. That is the honest rendering of "not for you".

---

### B · Users & access *(BACKED list, UNBACKED mutations)*

> **Screen: Users & access** at `/admin/users`. Two tabs: **Members** and **Roles**. This prompt
> is the Members tab; screen C is the Roles tab, and they share the header.
> Header: seat usage ("8 / 15 seats"), amber as it nears the cap. **A seat is a member or a
> pending invite** — say so where the number is, because the table below will be shorter than the
> count and a reader must not read that as a miscount.
> Table: avatar + name + mono handle, email, org role pill `OWNER / ADMIN / MEMBER`, per-project
> access summary (`core: write · web: read`), and **last write** with `no writes` as its own
> state. Below it, a **pending invites** section — email, role, invited by, expires in, copy
> accept-link, revoke — so the reserved seats are visible in the same screen as the taken ones.
> Row actions **change role** and **remove** are **UNBACKED** — no endpoint changes a role or
> removes a member today (PRD-21 §3.5, specified as D8, unbuilt). Draw them, and mark the two
> actions with the `NOT BACKED` chip rather than the whole screen. The owner row's actions are
> disabled regardless, with a tooltip saying the owner cannot be demoted.
> **States:** just-you (a new org); populated with two pending invites; at seat cap (invite
> disabled, with the plan that would raise it named); the remove confirmation, naming exactly
> what is lost.

---

### C · Roles & permissions *(UNBACKED — read the whole preamble before drawing)*

**This screen is why revision 2 exists, and it is the one most likely to produce a mockup nobody
can wire. The preamble is longer than the prompt on purpose.**

**Graphban already has three role systems.** A fourth must say how it relates to all three:

| System | Values | Where enforced | What it governs |
| --- | --- | --- | --- |
| **Org role** | `owner`, `admin`, `member` | `authz.require_org_admin` / `require_org_member` | Administering the org: invites, plan, settings. |
| **Project access** | `write`, `read`, `none` (on `Membership`) | `authz.can_read` / `can_write`, per project | Whether a human sees or edits a given project's data. |
| **Fleet agent role** | `planner`, `worker`, `reviewer` | `fleet.ROLES`, checked **at call time** | What an *agent* may do — claim, review, sign off. Enforced by credential; a prompt cannot launder it. |

The five roles in the last design — `OWNER / ADMIN / PRODUCT / DEVELOPER / ANALYST` — collapse
two different things into one list. `OWNER` and `ADMIN` are **authority**. `PRODUCT`, `DEVELOPER`
and `ANALYST` are **job function**. Permissions attach to the first; the second is a label that
tells a human who to ask. Both are legitimate and useful — they are not the same field, and a
role editor that pretends they are will produce a permission matrix with rows that cannot mean
anything.

**So the prompt puts the decision in front of you rather than guessing.** Draw **two variants**:

- **Variant 1 — Named presets over the axes that exist.** A role is a name plus a preset:
  one org role, plus a default per-project access level. `Developer` = member + write on all
  projects. `Analyst` = member + read. `Product` = member + write. No new enforcement, no policy
  engine — the role is a shortcut for two fields that already exist and are already checked.
  Cheap, shippable, and it gives the job-function labels a home.
- **Variant 2 — Custom roles with a permission matrix.** A role is a row; permissions are
  checkboxes. **If you draw this, every checkbox must name a real enforcement point.** The ones
  that exist are: read a project · write a project · invite members · administer the org ·
  assign project access · mint an API key · mint an enrolment seat · approve a memory shard ·
  publish a PRD baseline. Do not invent a tenth. A checkbox with no enforcement point behind it
  is the exact failure this set is written to prevent.

> **Screen: Roles.** The second tab of `/admin/users`, at `/admin/users/roles`. Left: the list of
> roles — name, a coloured dot, how many people hold it, and whether it is **built-in** (owner,
> admin, member — not editable, not deletable) or **custom**. Right: the selected role — its
> name, colour, description, and its grants, rendered per the variant.
> Built-in roles are shown with their permissions **read-only and explained**, not hidden: "owner
> is the account that created the org and cannot be demoted; there is exactly one."
> A **create role** action, and on every custom role a **delete** that first says how many members
> hold it and what they fall back to. Deleting a role people hold is the dangerous operation on
> this screen — make the fallback explicit, never implicit.
> Somewhere on the page, a plain sentence separating this from the fleet: "These are roles for
> people. Agent roles — planner, worker, reviewer — are assigned per agent in Fleet and enforced
> by credential, not by anything on this screen."
> **States:** built-in roles only (the default — three rows, nothing custom, and this must not
> look like an empty state, because it is a complete answer); one custom role selected; the
> create form; the delete confirmation naming affected members and their fallback; **a role held
> by nobody**, which is different from a role that was just created and should say which.

---

### D · Branding & theme *(UNBACKED)*

> **Screen: Branding** at `/admin/branding`. Header carries the `SPECULATIVE — NOT BACKED` chip;
> `Organization` has no branding columns today.
> Three sections, top to bottom:
> **1 · Logo.** A drop target with the current logo, or an empty state that is specific about what
> it wants: format, max dimensions, and that a transparent PNG or SVG reads best on the dark
> canvas. Show the uploaded logo previewed in the two places it will actually appear — the rail
> header at ~20px, and the sign-in card at ~40px — because a logo that works in one and dies in
> the other is the normal outcome and the screen should catch it before the user does.
> **2 · Palette.** A row of colour fields: **accent**, and the four status colours the product
> reserves (done / review / next / blocked). Two ways to fill them: pick manually, or
> **extract from the logo** — a button that pulls the dominant colours out of the uploaded image
> and offers them as candidates. Extraction **proposes; it never applies.** Show the candidate
> swatches with their hex values and an explicit accept, because a palette that changed itself
> when someone uploaded a logo is a surprise, not a feature.
> **Draw the contrast check.** A candidate accent lifted from a logo will often fail against
> `#0d0f0e`. Each swatch carries its contrast ratio against the canvas, and one that fails is
> marked and cannot be accepted — with the reason stated, not just a red border.
> **3 · Preview.** A live miniature of the rail, a card, and a status pill in the chosen palette,
> beside the same three in the default. Side by side, not a toggle: the comparison is the point.
> A reset-to-default action that says what it will discard.
> **States:** nothing customised (default palette, no logo — and it must read as *the default*,
> not as *unconfigured*); logo uploaded, palette untouched; extraction offering five candidates
> with one failing contrast; fully customised with the preview live.

---

### E · Custom domain *(UNBACKED — and the app is the small half)*

**Be honest about the cost in the prompt itself.** A custom domain is not a settings field. It is
DNS the customer controls, a TLS certificate somebody has to issue and renew, a reverse proxy
that routes by `Host`, and a tenant-resolution path that currently does not exist — today the org
comes from the session, never from the hostname. On a managed host it is also an API call to that
host per domain. The screen is a day; what it implies is not.

> **Screen: Custom domain**, a section of `/admin/branding` or its own page, carrying the
> `SPECULATIVE — NOT BACKED` chip.
> The whole screen is **one flow with four states, and the states are the design**:
> **1 · None.** What a custom domain gets you, the hostname you have today
> (`cloud.graphban.dev`), and an add-domain field.
> **2 · Pending DNS.** The entered hostname, and the exact records to create — rendered as a
> mono table of type / name / value with a copy on each, because this is the step where people
> fail. A **verify** action, a last-checked timestamp, and the specific reason the last check
> failed ("no CNAME found at `app.acme.dev`", not "verification failed").
> **3 · Issuing certificate.** DNS verified, TLS not yet. This is a real state that lasts minutes
> and must not look like an error or like success.
> **4 · Live.** The domain, its certificate expiry, and a remove action that states plainly that
> removing it will break every bookmark and every agent config pointing at it.
> Throughout: **the default hostname never stops working.** A custom domain is added, not
> swapped. Say so on the screen — it is the question every customer asks second.

---

### F · Integrations *(HALF — and the scope is wrong)*

> **Screen: Integrations** at `/admin/integrations`.
> **The mismatch to design around, not past:** GitHub and Google Drive connections exist today,
> but `PlatformConfig` is **one row per project** — there is no org-level connection. So an
> org-scoped Integrations page either (a) lists connectors with a per-project row underneath each,
> showing which projects are connected and which are not, or (b) is per-project and does not
> belong in the org admin section at all. **Draw (a)**, and make the per-project rows the visual
> weight of the screen rather than a detail: the honest shape of this feature is a matrix, not a
> list of on/off cards.
> Connected: the account, the target, and which projects use it. Not connected: a connect action.
> Jira, Linear, Confluence and Trello are **UNBACKED** and get the `SPECULATIVE — NOT BACKED`
> chip individually — they are PRD-23. GitHub and Google Drive do not.
> A note that a connected tracker raises a question of authority — which system owns an item's
> status — that PRD-23 settles and this screen does not.
> **States:** none connected; GitHub connected on one of three projects (the state that shows why
> this is a matrix); a connector mid-auth.

---

### G · Billing & usage *(HALF — display only)*

Unchanged in substance from screen 12 of the previous set; it moves under Admin and gains nothing.

> **Screen: Billing** at `/admin/billing`. Current plan card, and a **limits vs usage** table with
> exactly four rows, because these are the four counters that exist: **projects**, **seats**,
> **memory shards**, **MCP calls this month** — each a used/limit pair with a thin bar, amber near
> the cap, red at it. A plan comparison row (Free / Pro / Team / Enterprise) showing those same
> four limits per tier, with real numbers — enterprise is 500 / 1,000 / 1M / 10M, not unlimited.
> **Draw no payment method, no invoice list, no self-serve upgrade, and no usage chart.** Plans
> are operator-assigned and `OrgUsage` holds one row per period, so there is no time series to
> chart. The upgrade affordance is one line: "Contact your operator to change plan."
> Seats again reconcile members and pending invites.
> **States:** comfortable; one counter near cap; one at cap, naming what is now blocked.

---

### H · Operator console — corrections *(BACKED, shipped)*

The four operator screens are built. Redraw only if you are changing them; if so, these are the
corrections the shipped version already carries, and they should not regress:

- Route moves to `/operator/*`.
- The activity panel is the **operator ledger** — the four actions takeable from this plane
  (issue invite, revoke invite, decide org request, assign plan). It is not a tenant feed, and
  its empty state says "no operator has acted", never "the platform is quiet".
- **Last write**, not last active; `no writes` as its own state.
- Invite expiry and signup mode are **reported**, not editable — both are deployment env config.
- No `PURGE` on any row.
- The licensing screen carries the **additional-org request queue** the last set omitted; it is
  backed and it is the other half of licensing.
- Real plan limits, finite enterprise caps, org dots derived from id until branding lands.

---

## Part 4 — What Design cannot settle

These need a PRD answer before the unbacked screens can be built. They are listed here so the
mockups do not quietly answer them by drawing one option.

1. **Roles: variant 1 or variant 2?** Presets over the two axes that exist, or a custom-role
   subsystem with a permission table. Variant 2 is a policy engine and a migration; variant 1 is
   a lookup. Both are defensible; they are not the same size.
2. **D8 first, regardless.** Neither variant means anything until a role can be *changed* — there
   is no endpoint today. Member mutations are the floor.
3. **Where does a derived palette live?** Extraction runs client-side; the result is org state.
   Five columns on `Organization`, or one JSON blob? Precedent points at columns for things that
   are queried and JSON for things that are only rendered.
4. **Logo storage.** `Attachment` already stores bytes in the DB served public-read by unguessable
   id. A logo is public, so that fits — but it was built for feedback screenshots and has no
   owner column. Reuse it, or a sibling table with an `org_id`?
5. **Custom domain: which layer resolves the tenant?** Today the org comes from the session. Host-
   based resolution touches auth, CORS, cookie scope, invite links and the accept URL. This is
   the item to scope before it is drawn into a roadmap.
6. **Does `member` seeing no Admin group need an explanation anywhere?** A member who was told
   "go to Admin → Billing" finds nothing and no reason. A pointer ("ask an owner or admin") may
   belong in Settings.
