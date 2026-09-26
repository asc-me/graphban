"""Assemble graphban-local Claude Design cards from the captures in $DC_WORK (see README.md)."""
import json, re, os, html, pathlib, tempfile
S = pathlib.Path(os.environ.get("DC_WORK", pathlib.Path(tempfile.gettempdir()) / "gb-design-capture"))
OUT = S / "bundle"
REPO = pathlib.Path(os.environ.get("GB_REPO") or pathlib.Path(__file__).resolve().parents[2])
import datetime
DATE = datetime.date.today().isoformat()
def load(d, n):
    return json.load(open(S / d / f"{n}.json"))

CSS_CACHE = {}
def css_of(caps):
    return max((c["css"] for c in caps), key=len)

SCAFFOLD = """
html,body{height:auto!important;min-height:100%}
body{overflow:auto!important}
.ds-doc{padding:34px 34px 60px;font-family:var(--font-sans)}
.ds-doc>h1{font-size:22px;font-weight:600;letter-spacing:-.015em;margin:0 0 4px;color:var(--color-fg)}
.ds-lede{color:var(--color-muted);font-size:13px;line-height:1.55;max-width:96ch;margin:0 0 6px}
.ds-meta{font-family:var(--font-mono);font-size:10.5px;color:var(--color-faint);margin:0 0 26px;letter-spacing:.02em}
.ds-meta code,.ds-lede code{font-family:var(--font-mono);font-size:11.5px;color:var(--color-fg-2)}
.ds-vlabel{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--color-faint);font-family:var(--font-mono);margin:30px 0 4px}
.ds-vnote{font-size:12px;color:var(--color-muted);margin:0 0 10px;max-width:110ch;line-height:1.5}
.ds-frame{width:1440px;height:900px;overflow:hidden;position:relative;transform:translateZ(0);border:1px solid #2a3037;border-radius:14px;background:var(--color-bg)}
.ds-frame>#root,.ds-frame>div#root{height:100%}
.ds-strip{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}
.ds-box{border:1px dashed #2a3037;border-radius:12px;padding:18px;position:relative}
.ds-flag{border-left:3px solid var(--color-st-blocked);background:rgba(255,107,107,.06);padding:8px 12px;border-radius:6px;font-size:12px;color:var(--color-fg-2);margin:0 0 10px;max-width:110ch}
"""
FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">'

def page(path, group, title, lede, variants, meta=""):
    """variants: list of (label, note, capture_or_html, flag)"""
    caps = [v[2] for v in variants if isinstance(v[2], dict)]
    css = css_of(caps) if caps else css_of([load("out-self", "home")])
    parts = []
    for label, note, cap, flag in variants:
        body = cap["body"] if isinstance(cap, dict) else None
        parts.append(f'<div class="ds-vlabel">{html.escape(label)}</div>')
        if note: parts.append(f'<p class="ds-vnote">{note}</p>')
        if flag: parts.append(f'<div class="ds-flag">{flag}</div>')
        if body is not None:
            parts.append(f'<div class="ds-frame">{body}</div>')
        else:
            parts.append(cap)
    doc = f"""<!-- @dsCard group="{group}" -->
<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
{FONTS}
<style>
{css}
</style>
<style>{SCAFFOLD}</style>
</head>
<body>
<div class="ds-doc">
<h1>{html.escape(title)}</h1>
<p class="ds-lede">{lede}</p>
<p class="ds-meta">{meta}Captured from the running app (web/ on main, {DATE}) at 1440×900 — real DOM and compiled Tailwind, scripts removed. Regenerate, don't hand-edit.</p>
{''.join(parts)}
</div>
</body>
</html>
"""
    p = OUT / path / "index.html"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc)
    return path

SH = lambda n: load("out-self", n)
HO = lambda n: load("out-hosted", n)
EX = lambda n: load("out-extra", n)
UN = lambda n: load("out-unauth", n)
XX = lambda n: load("out-x", n)
POP = "Populated — self-host, seeded dataset (Core Platform)"
EMP = "Empty — hosted, fresh project (/p/GRPH/…)"
LOAD = "Loading — every data endpoint held open"
ERR = "Error — every data endpoint returns 500"
V = lambda l, c, note="", flag="": (l, note, c, flag)

written = []
def P(*a, **k): written.append(page(*a, **k))

# ---------------- Shell ----------------
P("shell/app-frame", "Shell", "App frame",
  "<code>AppFrame</code>: TopBar (search ⌘K, MCP tools-live pill, Agent toggle, user menu) · LeftNav (project switcher, Home, Plan / Build / Observe disclosures, footer Organization / Settings; hosted adds the Admin group, Galaxy, Organization, Operator, Feedback Kit) · main · AgentSidebar (Memory / Chat, mounts open).",
  [V("Self-host — Home", SH("home")),
   V("Hosted — Plan / Build / Observe + Admin group (GRPH-940)", HO("h-tracker"), "Hosted adds the org switcher, the project crumb bar (ITEMS · IN FLIGHT) and the Admin group; the flat list is collapsed into the same three sections as self-host."),
   V("Command palette — ⌘K", EX("command-palette")),
   V("Agent sidebar — Chat tab", EX("chat-tab"))])

# ---------------- Plan ----------------
P("pages/plan-home", "Plan", "Home",
  "Project health at a glance — NEEDS ATTENTION (in progress, review, blocked, memory waiting), inventory counts, and jump cards. Hosted has a per-project home at <code>/p/:tag</code>.",
  [V(POP, SH("home")), V("Hosted project home — /p/GRPH", HO("h-project-home")), V(LOAD, EX("home--loading")), V(ERR, EX("home--error"))])
P("pages/plan-tracker", "Plan", "Tracker",
  "One linear stream. Status filter chips, drag reorder, row → item drawer (status, fidelity/reach, description, linked code, dependencies, pull request, linked memory, assistant).",
  [V(POP, SH("tracker")), V("Item drawer open", EX("tracker-item-open")), V("New tracker item dialog", EX("tracker-new-item")),
   V(EMP, HO("h-tracker")), V(LOAD, EX("tracker--loading")), V(ERR, EX("tracker--error"))])
P("pages/plan-requests", "Plan", "Requests",
  "Triage queue from the public form. Type filter chips, votes, auto-duplicate links, publish / link actions; a row expands to linked code + operator comment.",
  [V(POP, SH("requests")), V("Row expanded", EX("requests-open")), V(EMP, HO("h-requests")), V(LOAD, EX("requests--loading")), V(ERR, EX("requests--error"))])
P("pages/plan-triage", "Plan", "Triage",
  "Incoming queue beside collision clusters — what came in, beside the in-flight work it would collide with.",
  [V(POP, SH("triage")), V(EMP, HO("h-triage")),
   V(LOAD, EX("triage--loading"), flag="GRPH-942 — defect as captured: while clusters are still loading the panel already says <b>Nothing in flight</b> in green — absence reads as clean."),
   V(ERR, EX("triage--error"), flag="GRPH-942 — defect as captured: on a 500 the queue says <b>Queue is empty</b> and clusters say <b>Nothing in flight</b>. Should say the data could not be loaded.")])
P("pages/plan-prds", "Plan", "PRDs",
  "Product specs with version history, linked items and AI drafting. List → editor (markdown + preview, Expand / Generate risks / Summarize / Grill, coverage, acceptance).",
  [V(POP, SH("prds")), V("PRD editor — CP-P1 Graphban Core MVP", EX("prd-editor")), V(EMP, HO("h-prds")), V(LOAD, EX("prds--loading")), V(ERR, EX("prds--error"))])
P("pages/plan-roadmap", "Plan", "Roadmap",
  "MVP → Post-MVP → Later. Progress rolls up from milestones; Copy public link.",
  [V(POP, SH("roadmap")), V(EMP, HO("h-roadmap")), V(LOAD, EX("roadmap--loading")),
   V(ERR, EX("roadmap--error"), flag="GRPH-943 — defect as captured: <code>/api/roadmap</code> 500 renders <b>No milestones in this project</b> — the same copy as a genuinely empty board (GRPH-924).")])

# ---------------- Build ----------------
P("pages/build-code-graph", "Build", "Code graph",
  "The codebase as agents described it — modules, files, symbols with typed relations; Ask-the-codebase panel.",
  [V("Self-host — nothing described yet", SH("code")), V("Hosted — nothing described yet", HO("h-code")), V(LOAD, EX("code--loading")),
   V(ERR, EX("code--error"), flag="GRPH-944 — defect as captured: <code>/api/agent/code/map</code> 500 renders <b>No code described yet</b> — “empty because nothing has been described”, which is not what happened.")])
P("pages/build-links", "Build", "Links",
  "Typed relationships between items and requests — dependency / code / semantic / tag edges; click a node or edge to inspect.",
  [V(POP, SH("links")), V(EMP, HO("h-links"))])
P("pages/build-fleet-v2", "Build", "Fleet.v2",
  "What can run and how you allocate it (GRPH-866): harness catalog (harness · model · tier · status · cost) and allocation by share of recent spawns. Roster and waves stay on Fleet.v1.",
  [V(POP, SH("fleet-v2")), V("Hosted", HO("h-fleet-v2")), V(LOAD, EX("fleet-v2--loading")), V(ERR, EX("fleet-v2--error"))])
P("pages/build-fleet-wave", "Build", "Fleet.v1",
  "Who is working here right now: Connections (roster, API keys that reach this project, legacy wave key), Wave, Work (items waiting for review).",
  [V("Connections — self-host", SH("fleet-v1")), V("Wave tab", XX("fleet-v1-wave")), V("Work tab", XX("fleet-v1-work")), V("Hosted", HO("h-fleet-v1"))])
P("pages/build-outposts", "Build", "Outposts",
  "Machines that have registered a gbagent/gbfleet agent on this project, and the harness they declared.",
  [V("Self-host — no host registered", SH("outposts")), V("Hosted", HO("h-outposts"))])
P("pages/observe-harness", "Observe", "Harness",
  "How each model and harness has turned out per capability and size band over 90 days (PRD-41 grid, ProbePanel, recommendations) + Harness preferences: your profile and project policy (PRD-37).",
  [V("Self-host — nothing measured yet", SH("harness")), V("Hosted", HO("h-harness")), V(LOAD, EX("harness--loading")), V(ERR, EX("harness--error"))])

# ---------------- Observe ----------------
P("pages/observe-memory-review", "Observe", "Memory review",
  "Agent-written memory is a candidate until you publish it; only published shards surface in search.",
  [V("Self-host — nothing to review", SH("memory-review")), V("Hosted", HO("h-memory-review")), V(LOAD, EX("memory-review--loading")), V(ERR, EX("memory-review--error"))])
P("pages/observe-lessons", "Observe", "Lessons",
  "Published memory scored against whether it is still catching anything — publishable / unmeasured / dropping, facet chips, outcome badges.",
  [V(POP, SH("lessons")), V(EMP, HO("h-lessons")), V(LOAD, EX("lessons--loading")), V(ERR, EX("lessons--error"))])
P("pages/observe-activity", "Observe", "Activity",
  "Every accepted mutation, attributed to the agent key or user that made it.",
  [V("Self-host — no activity yet", SH("activity")), V("Hosted", HO("h-activity"))])
P("pages/observe-live", "Observe", "Live",
  "Who is on this project right now, what they hold, and whether a PR was recorded.",
  [V("Self-host — no agents registered", SH("live")), V("Hosted", HO("h-live")), V(LOAD, EX("live--loading")), V(ERR, EX("live--error"))])

# ---------------- Org (hosted) ----------------
P("pages/org-overview", "Organization", "Organization",
  "Hosted org overview — projects, open items, graph nodes, MCP calls vs plan; Galaxy of cross-repo dependencies.",
  [V("Overview — /org", HO("h-org")), V("Galaxy — /org/galaxy", HO("h-galaxy"))])
P("pages/org-admin", "Organization", "Org admin",
  "Owner/admin only. Tabs: Users & access (members, roles, pending invites), Teams, Deployments, Branding (licensed), Integrations, Gitops, Billing.",
  [V("Users & access", HO("h-orgadmin-users")), V("Teams", HO("h-orgadmin-teams")), V("Deployments", HO("h-orgadmin-deployments")),
   V("Integrations", HO("h-orgadmin-integrations")), V("Gitops — house process", HO("h-orgadmin-gitops")), V("Billing", HO("h-orgadmin-billing"))])
P("pages/operator-console", "Operator", "Operator console",
  "Cross-tenant console at <code>/admin</code> for platform admins — its own cooler blue-black chrome (op-* tokens) so it is never mistaken for a tenant view.",
  [V("Platform", HO("h-op-home")), V("Orgs", HO("h-op-orgs")), V("Users", HO("h-op-users")), V("Licensing", HO("h-op-licensing"))])

# ---------------- Settings ----------------
P("pages/settings-this-box", "Settings", "Settings — This box",
  "Path-per-item settings. THIS BOX: AI providers, Cloud / Sync, Gitops, Updates.",
  [V("AI providers", SH("settings-providers")), V("Cloud / Sync", SH("settings-sync")), V("Gitops", SH("settings-gitops")), V("Updates", SH("settings-updates")),
   V("Hosted — Sync / Link (cloud side)", HO("h-settings-sync")), V("Hosted — AI providers", HO("h-settings-providers"))])
P("pages/settings-this-project", "Settings", "Settings — This project",
  "THIS PROJECT: Project, API keys, MCP Tools, Integrations, Feedback Kit, Members.",
  [V("Project", SH("settings-project")), V("API keys", SH("settings-api-keys")), V("MCP Tools", SH("settings-mcp")), V("Integrations", SH("settings-integrations")),
   V("Feedback Kit", SH("settings-feedback-kit")), V("Members", SH("settings-members")), V("Hosted — Project", HO("h-settings-project")), V("Hosted — API keys", HO("h-settings-api-keys"))])
P("pages/account-profile", "Settings", "Account & profile",
  "Account (password) and Profile (identity + project access).",
  [V("Settings → Account", SH("settings-account")), V("Profile", SH("profile")), V("Hosted — Profile", HO("h-profile"))])

# ---------------- Auth / onboarding / public ----------------
P("pages/auth-onboarding", "Auth", "Sign in & onboarding",
  "Unauthed surfaces and the hosted first-run: sign in, create account, reset password, invite, create your organization, create your first project.",
  [V("Sign in", UN("login")), V("Create account", UN("signup")), V("Reset password", UN("reset-password")), V("Invite — unavailable token", UN("invite-accept")),
   V("Hosted onboarding — create organization", UN("onboarding-create-org")), V("Hosted onboarding — first project", UN("onboarding-create-project"))])
P("pages/public-surfaces", "Public", "Public & embed",
  "Public pages served on the current origin (GRPH-901) and the embeddable widgets.",
  [V("Embed — feedback widget (light)", UN("embed-feedback")), V("Embed — roadmap", UN("embed-roadmap")), V("Public feedback form", UN("public-feedback")),
   V("Public board — not found", UN("public-issues")), V("Public roadmap — not found", UN("public-roadmap")), V("Tracking link — not found", UN("public-tracking"))])


# ---------------- Foundations & components ----------------
src = open(REPO / "web/src/index.css").read()
theme = src[src.index("@theme {"): src.index("@keyframes alFade")]
vars_ = re.findall(r"--([\w-]+):\s*([^;]+);(?:\s*/\*\s*(.*?)\s*\*/)?", theme, re.S)

def sw(name, val, note=""):
    return f'<div style="width:150px"><div style="height:56px;border-radius:10px;background:{val};box-shadow:0 0 0 1px rgba(255,255,255,.08) inset"></div><div class="font-mono" style="font-size:11px;color:var(--color-fg-2);margin-top:6px">--{name}</div><div class="font-mono" style="font-size:10.5px;color:var(--color-faint)">{html.escape(val.strip())}</div></div>'
groups = {"Surfaces & text": [], "Lines": [], "Accent & brand": [], "Status": [], "Item type": [], "Operator plane (op-*)": []}
type_rows, radius, shadows, motion = [], [], [], []
for n, v, c in vars_:
    v = " ".join(v.split())
    if n.startswith("color-op-"): groups["Operator plane (op-*)"].append(sw(n, v))
    elif n.startswith("color-st-"): groups["Status"].append(sw(n, v))
    elif n.startswith("color-ty-"): groups["Item type"].append(sw(n, v))
    elif n.startswith("color-line"): groups["Lines"].append(sw(n, v))
    elif n.startswith(("color-accent", "color-focus", "color-purple")): groups["Accent & brand"].append(sw(n, v))
    elif n.startswith("color-"): groups["Surfaces & text"].append(sw(n, v))
    elif n.startswith("text-") and "--" not in n:
        type_rows.append(f'<div style="display:flex;align-items:baseline;gap:24px;padding:10px 0;border-bottom:1px solid #1e242a"><div class="font-mono" style="width:180px;font-size:11px;color:var(--color-faint)">--{n} · {v}</div><div class="text-{n.split("-",1)[1]} {"font-mono" if n=="text-micro" else ""}" style="color:var(--color-fg)">{"GRPH-927 · gb_live_7f3a" if n=="text-micro" else "Agents build; the tracker remembers"}</div><div style="font-size:11px;color:var(--color-muted);margin-left:auto">{html.escape(c or "")}</div></div>')
    elif n.startswith("radius-"): radius.append(f'<div style="text-align:center"><div style="width:96px;height:64px;border-radius:{v};background:var(--color-surface-3);box-shadow:var(--shadow-elev-1)"></div><div class="font-mono" style="font-size:10.5px;color:var(--color-faint);margin-top:6px">--{n} · {v}</div><div style="font-size:10.5px;color:var(--color-muted)">{html.escape(c or "")}</div></div>')
    elif n.startswith("shadow-"): shadows.append(f'<div style="text-align:center"><div style="width:160px;height:96px;border-radius:12px;background:var(--color-surface-2);box-shadow:var(--{n})"></div><div class="font-mono" style="font-size:10.5px;color:var(--color-faint);margin-top:10px">--{n}</div></div>')
    elif n.startswith(("ease-", "duration-", "animate-")): motion.append(f'<tr><td class="font-mono" style="padding:6px 16px 6px 0;color:var(--color-fg-2);font-size:11.5px">--{n}</td><td class="font-mono" style="padding:6px 0;color:var(--color-faint);font-size:11px">{html.escape(v)}</td></tr>')
blocks = []
for g, items in groups.items():
    blocks.append(f'<div class="ds-vlabel">{g}</div><div class="ds-strip">{"".join(items)}</div>')
blocks.append('<div class="ds-vlabel">Type scale — ≤6 roles, each ≥1.2× the step below (GRPH-908). Plex Sans for chrome; mono only for IDs and keys (GRPH-938)</div><div style="max-width:1100px">' + "".join(type_rows) + '</div>')
blocks.append('<div class="ds-vlabel">Radius — control / card / overlay</div><div class="ds-strip" style="gap:28px">' + "".join(radius) + '</div>')
blocks.append('<div class="ds-vlabel">Elevation — material, not outlines (GRPH-937)</div><div class="ds-strip" style="gap:36px;padding:20px 0">' + "".join(shadows) + '</div>')
blocks.append('<div class="ds-vlabel">Motion (PRD-46 §6)</div><table>' + "".join(motion) + '</table>')
blocks.append('<div class="ds-vlabel">Ground</div><p class="ds-vnote">Body: two radial washes (purple 6% top-right, lime 5% bottom-left) over --color-bg, plus a 3.5% SVG film-grain overlay on the page (never on individual cards). Focus ring: 2px --color-focus, 2px offset.</p>')
home = SH("home")
written.append(page("foundations/tokens", "Foundations", "Tokens", "Graphban design tokens as defined in <code>web/src/index.css</code> <code>@theme</code> — dark only. Supersedes the 09-08 card (lines are now lighter, surfaces warmer, and a type/radius/elevation/motion scale exists).",
    [("", "", "\n".join(blocks), "")]))
# fix empty vlabel emitted for single variant
p = OUT / "foundations/tokens/index.html"; p.write_text(p.read_text().replace('<div class="ds-vlabel"></div>', '', 1))

def ex(n): return json.load(open(S / "out-x" / f"{n}.extract.json"))
t, r, sp, hz, ni, hm = ex("x-tracker"), ex("x-requests"), ex("x-settings-project"), ex("x-harness"), ex("x-newitem"), ex("x-home")
def box(label, inner, style=""):
    return f'<div class="ds-vlabel">{label}</div><div class="ds-box" style="{style}">{inner}</div>'
bi = [
 box("Primary button — New item / Save", '<div class="ds-strip">' + "".join(t["btn-primary"] + hz["btn-primary"][:1]) + '</div>'),
 box("Outline / small action buttons", '<div class="ds-strip">' + "".join(r["outline-btn"][:4]) + '</div>'),
 box("Top-bar search", "".join(t["search"]), "width:420px"),
 box("Text inputs", '<div style="display:flex;flex-direction:column;gap:10px;width:520px">' + "".join(sp["input"]) + "</div>"),
 box("Textarea", "".join(sp["textarea"]), "width:720px"),
 box("Select", '<div class="ds-strip">' + "".join(hz["select"]) + "</div>"),
 box("Checkboxes", '<div style="display:flex;flex-direction:column;gap:8px;width:720px">' + "".join(sp["checkbox"][:4]) + "</div>"),
 box("Radio group — agent memory writes", '<div style="display:flex;flex-direction:column;gap:8px;width:720px">' + "".join(sp["radio"]) + "</div>"),
]
cm = [
 box("Status filter chips", '<div class="ds-strip">' + "".join(t["filter-chips"]) + "</div>"),
 box("Item-type badges", '<div class="ds-strip">' + "".join(r["type-badges"]) + "</div>"),
 box("Tabs (radix) — agent sidebar", "".join(t["tabs"]), "width:340px"),
 box("Left-nav items", '<div style="display:flex;flex-direction:column;gap:2px;width:216px">' + "".join(t["nav-item"]) + "</div>"),
 box("Tracker rows", '<div style="width:900px">' + "".join(t["row"][:4]) + "</div>"),
 box("Dialog — New tracker item", '<div style="height:420px;position:relative;transform:translateZ(0)">' + "".join(ni["dialog"]) + "</div>", "width:640px"),
]
caps = [XX("x-tracker"), XX("x-requests"), XX("x-settings-project"), XX("x-harness"), XX("x-newitem"), XX("x-home")]
def comp(path, title, lede, blocks):
    page(path, "Components", title, lede, [("", "", "\n".join(blocks), "")])
    p = OUT / path / "index.html"; txt = p.read_text().replace('<div class="ds-vlabel"></div>', '', 1)
    # component cards need the full app CSS (union of the captures they came from)
    txt = re.sub(r"<style>\n.*?\n</style>", lambda m: "<style>\n" + css_of(caps) + "\n</style>", txt, count=1, flags=re.S)
    p.write_text(txt); written.append(path)
comp("components/buttons-inputs", "Buttons & inputs", "Real markup lifted from the running app (Tracker, Requests, Settings → Project, Harness).", bi)
comp("components/chips-tabs-menus", "Chips, tabs, rows & dialog", "Real markup lifted from the running app.", cm)
json.dump(written, open(S / "written.json", "w")); print(len(written))
