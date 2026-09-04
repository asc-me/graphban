/** Context-aware content for the inline docs reader, keyed by route.
 *  Distilled from the docs/ guides into the reader's structured format. */

import { ORG_BASE, adminPath, orgPath, settingsPath } from "@/lib/routes";

export interface DocSection {
  num: number;
  h: string;
  b: string;
}
export interface DocShortcut {
  k: string;
  d: string;
}
export interface DocRelated {
  label: string;
  to: string;
}
export interface DocEntry {
  badge: string; // mono page label in the header
  title: string;
  tagline: string;
  sections: DocSection[];
  shortcuts?: DocShortcut[];
  related?: DocRelated[];
}

const DEFAULT: DocEntry = {
  badge: "GRAPHBAN",
  title: "Graphban",
  tagline: "Agent memory. Linear execution.",
  sections: [
    { num: 1, h: "One linear stream", b: "Work lives in a single priority-ordered tracker, not boards. The left nav switches between the workspace views." },
    { num: 2, h: "Agent context", b: "The right sidebar (toggle “Agent”) holds searchable memory and a chat grounded in your project state." },
    { num: 3, h: "Same code path for agents", b: "Agents drive the app through MCP tools — identical to the UI, so their writes show up instantly." },
  ],
  related: [
    { label: "MCP Tools", to: "/mcp-tools" },
    { label: "Dashboard", to: "/dashboard" },
  ],
};

const CONTENT: Record<string, DocEntry> = {
  default: DEFAULT,

  "/tracker": {
    badge: "TRACKER",
    title: "Tracker",
    tagline: "One linear stream of work, priority + recency.",
    sections: [
      { num: 1, h: "Advance status", b: "Click a status dot on a row (or in the detail panel) and pick a new state. Moving an item to Done auto-extracts a lesson into memory." },
      { num: 2, h: "Reorder", b: "Drag a row to change its position — the new order persists." },
      { num: 3, h: "Filter & inspect", b: "Use the status chips to filter and the top-bar search to find by title or id. Click a row for its detail panel: description, blocker, PR, and linked memory." },
      { num: 4, h: "Graduation checklist", b: "Saving a gitops model files a checklist on this stream: observe the remote, confirm HEAD, stop pushing to base, open one PR, prove the contract live, first tagged cut. Those are process, not code — they stall in review until evidenced (P23). Graphban does not run git." },
    ],
    related: [
      { label: "MCP Tools", to: "/mcp-tools" },
      { label: "Dashboard", to: "/dashboard" },
      { label: "Gitops", to: settingsPath("deployment/gitops") },
    ],
  },

  "/requests": {
    badge: "REQUESTS",
    title: "Requests",
    tagline: "Triage queue fed by the public feedback form.",
    sections: [
      { num: 1, h: "Triage", b: "Filter by type, upvote, and link a request to a tracker item — linking sets its status to “linked”." },
      { num: 2, h: "Public submissions", b: "The embeddable feedback widget (/embed/feedback) drops submissions here, with auto-duplicate detection before they land." },
    ],
    related: [{ label: "Feedback Kit", to: "/feedback-kit" }],
  },

  "/prds": {
    badge: "PRDS",
    title: "PRDs",
    tagline: "Specs with versions, links, and AI drafting.",
    sections: [
      { num: 1, h: "Create from a template", b: "New PRD offers a Standard skeleton or Blank. Each row shows status, version, and linked items." },
      { num: 2, h: "Open to edit", b: "Click a PRD to open the split markdown editor with live preview, version history, and AI commands." },
    ],
  },

  "prd-editor": {
    badge: "PRD EDITOR",
    title: "PRD editor",
    tagline: "Markdown editor with preview, versions, and AI.",
    sections: [
      { num: 1, h: "Write with live preview", b: "Edit markdown on the left; the rendered preview updates on the right. Save persists your draft." },
      { num: 2, h: "Version & diff", b: "Snapshot cuts a new version and bumps the number. The History tab lists versions; click one for a line diff against your draft." },
      { num: 3, h: "AI commands", b: "Expand, Generate risks, and Summarize append generated markdown to the body — using the configured chat provider." },
      { num: 4, h: "Link items", b: "The Linked dropdown attaches tracker items to this PRD." },
      { num: 5, h: "Coverage", b: "The Coverage tab shows task gaps on buildable sections, and empty headings separately. A body with no sections is not a clean pass. Empty is not a score. Fill gaps files tasks only after the grill earns approved — a draft is not a task list." },
      { num: 6, h: "Readiness to approve", b: "On the grill tab. A warning, not a gate — the grill still earns approved. Mechanical checks (present vs placeholder, empty headings, task gaps) load without a model. Ask the judge scores ambiguity and testability. Ungraded is not a fail; not asking is not a pass." },
    ],
    related: [{ label: "Tracker", to: "/tracker" }],
  },

  "/links": {
    badge: "LINKS",
    title: "Links graph",
    tagline: "Typed relationships between items and requests.",
    sections: [
      { num: 1, h: "Read the graph", b: "Nodes are items and requests; edges are colored by type — dependency, code, semantic, tag. Toggle the type chips to filter." },
      { num: 2, h: "Inspect", b: "Click a node to highlight its neighborhood and see each connection’s reason; click an edge for its endpoints and confidence." },
    ],
  },

  "/home": {
    badge: "HOME",
    title: "Home",
    tagline: "Project health at a glance — items, memory, and who is in flight.",
    sections: [
      { num: 1, h: "Counts, not a fetch of everything", b: "Tiles come from the dashboard payload and the shell counts endpoint. Fetching items to call .length is how this page once cost megabytes on every route." },
      { num: 2, h: "Attention first", b: "Blocked, in review, and live agents sit above the rest. Stale counts say stale — last good numbers, not a quiet zero." },
    ],
    related: [{ label: "Tracker", to: "/tracker" }],
  },

  "/dashboard": {
    badge: "DASHBOARD",
    title: "Dashboard",
    tagline: "Project health at a glance.",
    sections: [
      { num: 1, h: "KPIs & distribution", b: "Tiles summarize items, memory, PRDs, and MCP activity. The status bar shows the spread across the six states with a labeled legend." },
      { num: 2, h: "Requests & activity", b: "See requests broken down by type and the most recently updated items." },
    ],
    related: [{ label: "Tracker", to: "/tracker" }],
  },

  "/triage": {
    badge: "TRIAGE",
    title: "Triage",
    tagline: "What came in, and what a new claim would collide with.",
    sections: [
      { num: 1, h: "Per-project only", b: "Collision clustering reasons over this project's code graph. A package name shared across repos is a galaxy edge, not a collision — this screen does not compute that." },
      { num: 2, h: "Incoming and clusters", b: "The left queue is untriaged requests. The right is non-colliding work already in flight. An empty queue is not 'nothing was ever reported' — history lives in the tracker." },
    ],
    related: [
      { label: "Requests", to: "/requests" },
      { label: "Fleet", to: "/fleet" },
    ],
  },

  "/code": {
    badge: "CODE GRAPH",
    title: "Code graph",
    tagline: "Symbols, files, and who holds them.",
    sections: [
      { num: 1, h: "Kinds and edges", b: "Modules, files, symbols, plus muted docs and config. Edges: imports, calls, owns, tested-by, references. Toggle types; collapse components when the graph is too wide." },
      { num: 2, h: "Held areas are the alarm", b: "Overlapping clouds are two agents on the same files. The fleet legend names who holds what. An area this graph cannot place is still reported — not dropped." },
    ],
    related: [{ label: "Fleet", to: "/fleet" }],
  },

  "/fleet": {
    badge: "FLEET",
    title: "Fleet",
    tagline: "Agents on this project, what they hold, and who must review.",
    sections: [
      { num: 1, h: "Roster and posture", b: "Offline agents fade rather than vanish — one that died holding a branch is what you need to see. Fleet posture: specialised roles review themselves. Single-agent: you are the reviewer." },
      { num: 2, h: "Review queue and clusters", b: "The queue names who built each item — the reason it needs somebody else. Clusters are non-colliding work; anything held back says why." },
      { num: 3, h: "Waves and seats", b: "A seat is the role for this session — paste it into the prompt, not the MCP config. Ending a wave is irreversible and names which wave. The API key that authenticates is minted on Settings → API keys; a wave key minted here is swept by End wave." },
    ],
    related: [{ label: "API keys", to: settingsPath("project/api-keys") }],
  },

  "/roadmap": {
    badge: "ROADMAP",
    title: "Roadmap",
    tagline: "Phased milestones with rolled-up progress.",
    sections: [
      { num: 1, h: "Phases", b: "MVP → Post-MVP → Later. Each phase’s progress is computed from its milestones’ done state." },
      { num: 2, h: "Share it", b: "Copy public link gives a read-only /embed/roadmap page you can share outside the app." },
    ],
  },

  "/mcp-tools": {
    badge: "MCP TOOLS",
    title: "MCP Tools",
    tagline: "The tool surface agents call — with live metrics.",
    sections: [
      { num: 1, h: "The surface", b: "57 live tools over a JSON-RPC endpoint (POST /api/mcp) — 34 core, shipped to every key, the rest by tier. Each card shows its params, description, and call count." },
      { num: 2, h: "Authenticate agents", b: "Mint an API key in Settings → API keys, then call the endpoint with it. Looking for API keys? is that mint — Graphban keys, not an LLM secret. A seat on Fleet is a role for one session, not a key. Every call is metered here." },
      { num: 3, h: "Where keys come from", b: "Two paths, one key shape. Mint on Settings → API keys, or provision the first one without a browser: `graphban init` runs once on a virgin instance and creates the operator, the project, and its first key — `--key-scope` and `--key-tiers` are the same axes as the mint dialog. This page is the catalog; it does not mint keys." },
      { num: 4, h: "Scope and tiers", b: "A Project-scoped key points the agent's writes at one project; a Global key is unbound, so calls pass `project_id` (or fall back to the default). The connect snippets follow the key: where the harness supports scoping (Claude Code, Grok CLI), a project key gets `--scope project` and a global key `--scope user`. Tiers (prd / codegraph / fleet / misc) decide which specialist tools the agent can SEE in its manifest — visibility, not permission: scopes and roles decide what may be called. Keys are shown once and stored only as a hash; a lost key is a new mint." },
    ],
    related: [
      { label: "API keys", to: settingsPath("project/api-keys") },
      { label: "Fleet", to: "/fleet" },
    ],
  },

  "/memory-review": {
    badge: "MEMORY REVIEW",
    title: "Memory review",
    tagline: "Approve agent-written memory before it's trusted.",
    sections: [
      { num: 1, h: "Candidates, not truth", b: "Memory an agent writes (via add_memory or auto-extracted on Done) enters as a candidate. It stays out of the default semantic search until you publish it here." },
      { num: 2, h: "Publish or reject", b: "Publish promotes a shard into the trusted retrieval path every future agent searches. Reject keeps it for provenance but never surfaces it. Both are recorded in Activity." },
      { num: 3, h: "Recurring lessons", b: "When the same correction shows up several times, it's grouped as a recurring lesson — publish it once as a principle and drop the duplicates in a single action." },
      { num: 4, h: "The LLM judge", b: "When the LLM judge is on, opening the queue scores groundedness and readiness (capped). Similarity is always there. A missing list score means the toggle is off, the cap was hit, or the model could not decide — never that the note is fine. Ask the judge re-asks keep/quality for one candidate; it does not replace the list scores." },
    ],
    related: [
      { label: "Lessons", to: "/lessons" },
      { label: "Activity", to: "/activity" },
      { label: "Project", to: settingsPath("project") },
    ],
  },

  "/lessons": {
    badge: "LESSONS",
    title: "Lessons",
    tagline: "Published memory, scored against whether it is still catching anything.",
    sections: [
      { num: 1, h: "Catalog, not inbox", b: "This is the published set. Candidates stay in Memory review until you stand behind them. An empty catalog is not a high score." },
      { num: 2, h: "Unknown is a real state", b: "Caught-issues and effectiveness stay unknown until something is counted. Quiet time does not raise the score. A missing measurement is unverifiable, not ineligible." },
      { num: 3, h: "Org promotion", b: "The Promote control is always on the detail page when the lesson is still project-reach. It shows the real reason it is disabled. Unverifiable cannot be overridden. There is no separate org-catalog page in this slice — a successful promote is visible on sibling project lists." },
    ],
    related: [
      { label: "Memory review", to: "/memory-review" },
      { label: "Activity", to: "/activity" },
    ],
  },

  "/activity": {
    badge: "ACTIVITY",
    title: "Activity",
    tagline: "The audit ledger — who did what, when.",
    sections: [
      { num: 1, h: "Every mutation", b: "One row per accepted change, attributed to the agent API key or user that made it. Agent (MCP) actions and user (REST) actions both land here." },
      { num: 2, h: "Scoped to your projects", b: "You see activity only for projects you can read. Switch project in the left nav to narrow it." },
    ],
    related: [{ label: "MCP Tools", to: "/mcp-tools" }],
  },

  "/live": {
    badge: "LIVE",
    title: "Live",
    tagline: "Who is on this project right now.",
    sections: [
      { num: 1, h: "Grouped by human", b: "Each person is one row of agents. Filter by user; Unattributed is a real bucket when a key has no user. All includes them." },
      { num: 2, h: "Leases, not writes", b: "Files are area leases. An online agent holding work with no lease is unreserved, not idle. Predicted and off-map are labelled. Graphban cannot see the agent's disk." },
      { num: 3, h: "Recorded PRs only", b: "A PR appears when Graphban already stored a URL on a holding. unrecorded is a word, not an empty list. Graphban does not fetch git." },
      { num: 4, h: "What it is doing", b: "Every call an agent makes to Graphban is a row, reads included, under its own name. Click a row for the timeline. Calls that named no agent are counted on the credential, never guessed onto one. 'No calls recorded' is a measurement, not a blank." },
    ],
    related: [
      { label: "Fleet", to: "/fleet" },
      { label: "Code graph", to: "/code" },
      { label: "Activity", to: "/activity" },
    ],
  },

  "/feedback-kit": {
    badge: "FEEDBACK KIT",
    title: "Feedback Kit",
    tagline: "Generate a themeable embeddable feedback widget.",
    sections: [
      { num: 1, h: "Configure", b: "Pick an accent, corner radius, enabled types, and whether to collect email — the preview updates live." },
      { num: 2, h: "Embed", b: "Copy the iframe snippet. The widget posts to the public feedback endpoint with built-in duplicate detection." },
    ],
    related: [{ label: "Requests", to: "/requests" }],
  },

  "/settings": {
    badge: "SETTINGS",
    title: "Settings",
    tagline: "Providers, integrations, project, members, keys.",
    sections: [
      { num: 1, h: "AI Providers (This box)", b: "Under the deployment section — providers are what the box runs on, not per-project config. Add a credential for Anthropic, OpenAI, Gemini, Grok, Groq, DeepSeek, Mistral, Ollama, Qwen, Kimi, GLM, MiniMax, OpenRouter, Together, Fireworks, Perplexity, Cohere, or any custom OpenAI-compat endpoint; every compat entry carries an editable endpoint so a gateway or local server is one field, not a code change. Embeddings stay a deploy-time setting." },
      { num: 2, h: "Integrations", b: "Connect GitHub/Drive config and copy the inbound issues webhook — opened GitHub issues become tracker items." },
      { num: 3, h: "Project, members, keys", b: "Edit project config and flags, review member roles, and mint/revoke API keys (shown once). The mint dialog asks for scope — Project pins the agent's writes to the active project, Global lets the agent name a project per call — and which tool tiers the key advertises. The same axes exist for the first-run key as `graphban init --key-scope` / `--key-tiers`." },
    ],
    related: [{ label: "MCP Tools", to: "/mcp-tools" }],
  },

  "/settings/project/api-keys": {
    badge: "API KEYS",
    title: "API keys",
    tagline: "Graphban keys — not LLM credentials.",
    sections: [
      { num: 1, h: "Three kinds", b: "This page mints Graphban keys. LLM credentials (what the box runs on) live on AI Providers — Looking for LLM credentials? is that question. An agent key talks to MCP — read and write on items, memory, and code. A link key pushes a code graph from a local box into exactly one project and nothing else — same object Sync / Link mints. The name field is Link key name, not Key name. A gate key attests that work was checked so an item may reach done: for CI or a reviewer, never for the agent doing the work. Gate is minted with read+write+gate; gate alone cannot attest." },
      { num: 2, h: "Scope is the write target", b: "Project pins the agent's writes to the active project. Global is unbound — calls pass project_id or fall back to the default. Connect snippets follow the key: Claude Code and Grok CLI get --scope project or --scope user. Sync and gate always pin to one project. The picker is Link key target project, not Sync target — same label Cloud / Sync uses." },
      { num: 3, h: "Advertisement is not permission", b: "An agent key ships the core tools. Tiers (PRDs, code-graph writes, fleet admin, occasional) opt in specialist tools because they cost manifest tokens every turn. A tool left out of tools/list is still callable; scopes and roles decide what may be called. Visibility is not the gate. The symptom of a missing tier is an agent that does not know a tool exists, never an error. The Fleet admin tier is for the supervisor or planner key — being in a fleet needs a seat, not this tier." },
      { num: 4, h: "Shown once", b: "The plaintext is shown once and stored as a hash. A lost key is a new mint. Two paths, one key shape: mint here, or graphban init --key-scope / --key-tiers on a virgin instance." },
      { num: 5, h: "Minted with", b: "After the banner, Minted with on each row opens what that key was minted with: scopes, advertised MCP tool groups and how many tools its tools/list carries, project/global, expiry. The count is the server's, from the same manifest the key is shipped, so a key that shows 34 tools has no tier, whatever was intended at mint. Tools are listed for agent keys only — a link key and a gate key do not call MCP, so a tools line on them would name a capability they do not have. core is always named; no extra tiers is core-only, not missing." },
      { num: 6, h: "Seats are not keys", b: "A seat on Fleet grants a role for one session and expires. Put the API key in MCP config once; issue a seat per agent per wave. A wave-tagged key minted on Fleet is swept by End wave — it is labelled here so it cannot look like a hand-minted key." },
    ],
    related: [
      { label: "MCP Tools", to: settingsPath("project/mcp") },
      { label: "Fleet", to: "/fleet" },
      { label: "AI providers", to: settingsPath("deployment/providers") },
    ],
  },

  "/settings/project/providers": {
    badge: "AI PROVIDERS",
    title: "AI providers",
    tagline: "Credentials this box runs on — not Graphban API keys.",
    sections: [
      { num: 1, h: "This box, not this project", b: "Self-host: This box → AI providers. Hosted: the AI Providers tab. An LLM credential is what the instance runs on. The blurb is every LLM credential configured on this deployment — not every provider. Provider is the catalogue (Anthropic). The secret on a credential is a Provider key. The Label field is the credential's name — Anthropic credential, not Anthropic key. Looking for API keys? is Graphban identity. Agent keys that call Graphban live on API keys." },
      { num: 2, h: "Catalogue plus a custom endpoint", b: "Pick Anthropic, OpenAI, Gemini, Grok, Groq, DeepSeek, Mistral, Ollama, Qwen, Kimi, GLM, MiniMax, OpenRouter, Together, Fireworks, Perplexity, Cohere, or Custom. Every OpenAI-compat row shows an editable endpoint, pre-filled from the catalogue so the default URL is zero-typing and a gateway or local server is one field." },
      { num: 3, h: "Deployment default, then project", b: "A credential can be the box default. A project may point at one; if that pointer is dead, resolution falls back to the deployment default rather than going silent." },
      { num: 4, h: "Task models", b: "Classify, critique, and the memory judge can use a different credential. Unset inherits this project's chat — a missing role is not 'no judge'. A named credential that cannot be used is ungraded, not a quieter model." },
      { num: 5, h: "Health is of the credential", b: "The dot is labelled, not colour-only. Pending validation is not unreachable — nobody asked yet. Test connection is the ask; pending cannot be default. Unreachable stays selectable — it was asked and did not answer. A wrong model name is refused with the list the provider actually offers." },
    ],
    related: [{ label: "API keys", to: settingsPath("project/api-keys") }],
  },

  "/settings/deployment/sync": {
    badge: "CLOUD / SYNC",
    title: "Cloud / Sync",
    tagline: "A link key from your cloud org is what a local box pastes.",
    sections: [
      { num: 1, h: "Mint the key in the cloud org", b: "On the hosted org: Settings → Sync / Link, or API keys → Link key. Scope is sync, pinned to one project — that is the only project the box can push. The name field is Link key name, not Key name — the deployment's identity on Deployments. Shown once." },
      { num: 2, h: "Paste it on the local box", b: "Self-host Settings → Cloud / Sync takes the cloud URL (the org origin) and the Link key — the paste field is not a credential. The intro and placeholder name the link key, not paste key. The link key is stored encrypted — the same handling as provider keys. Tenant/org is a label. graphban link --cloud-url --api-key --project is the same hand-off; --api-key is the flag, not a second name." },
      { num: 3, h: "The box pushes; the org does not reach in", b: "The local box builds the graph and pushes summaries. Vectors stay on the box; the cloud re-embeds. Nothing is linked until that paste happens." },
    ],
    related: [{ label: "API keys", to: settingsPath("project/api-keys") }],
  },

  "/settings/project": {
    badge: "PROJECT",
    title: "Project",
    tagline: "Flags for this project — memory, MCP, and who may sign off.",
    sections: [
      { num: 1, h: "Memory write modes", b: "Review: agent writes wait as candidates. Auto: only corroborated lessons publish. Trusted: publishes on write, labelled and undoable. An empty catalog is not a high score." },
      { num: 2, h: "Danger mode is conditional", b: "Allow self-review only applies when no other agent could have reviewed the item. With a second agent here, self-review is still refused. Items signed this way say so." },
    ],
    related: [{ label: "Memory review", to: "/memory-review" }],
  },

  "/settings/project/integrations": {
    badge: "INTEGRATIONS",
    title: "Integrations",
    tagline: "GitHub, Drive, and the inbound issues webhook — per project.",
    sections: [
      { num: 1, h: "Connectors on this project", b: "GitHub and Google Drive are real. The inbound issues webhook turns opened GitHub issues into tracker items." },
      { num: 2, h: "Spam on the public form", b: "Rate limit and Turnstile live here because the feedback widget posts to a public endpoint." },
    ],
    related: [{ label: "Feedback Kit", to: settingsPath("project/feedback-kit") }],
  },

  "/settings/project/members": {
    badge: "MEMBERS",
    title: "Members",
    tagline: "People with access to this project, and their roles.",
    sections: [
      { num: 1, h: "Role and access", b: "Each row is a person, their role, and whether they can write. This is project membership, not the org seat table." },
    ],
    related: [{ label: "Project", to: settingsPath("project") }],
  },

  "/settings/account": {
    badge: "ACCOUNT",
    title: "Account",
    tagline: "Your password on this box.",
    sections: [
      { num: 1, h: "Changing it signs out the rest", b: "That is the point, not a side effect. There is no recovery path that leaves other sessions alive." },
    ],
    related: [{ label: "Profile", to: "/profile" }],
  },

  "/settings/deployment/updates": {
    badge: "UPDATES",
    title: "Updates",
    tagline: "Whether this box is on the published stable cut.",
    sections: [
      { num: 1, h: "Three states", b: "current, available, or unknown. Current says this box is on the latest release. Unknown is not current — a failed feed fetch or a placeholder 0.1.0 version must not look up to date." },
      { num: 2, h: "Check and Install", b: "Check for updates refetches the feed. Install is enabled when a compose host helper is on the unix socket, or this is a native /opt/graphban install, and a newer cut is advertised. The asset is graphban-<tag>.tar.gz — GitHub's source zip is not a release. Stamp and publish cut a release; they do not apply. Install is the operator gate. The API does not get a Docker socket. Hosted has Check, no Install. How this box is deployed is named: compose helper, native, hosted, or could not tell — empty via is not compose." },
      { num: 3, h: "Release notes", b: "An accordion of this cut's GitHub Release body. When an update is available, a second list is the latest tag — not a rollup of skipped cuts. Empty body is no notes on this release; a failed fetch is could not load notes. Those are not the same." },
      { num: 4, h: "This box, not the repo", b: "This page is the instance — whether this Graphban is on the published stable cut. The repo's delivery contract is Gitops, a sibling under This box. They are not the same record." },
    ],
    related: [
      { label: "Gitops", to: settingsPath("deployment/gitops") },
      { label: "Tracker", to: "/tracker" },
    ],
  },

  "/settings/deployment/gitops": {
    badge: "GITOPS",
    title: "Gitops",
    tagline: "This project's delivery contract — unmeasured is not main.",
    sections: [
      { num: 1, h: "Unmeasured, not main", b: "Unset fields are unmeasured — not 'use main' and not 'no requirements'. Agents read the resolved contract from get_context." },
      { num: 2, h: "Named models", b: "Push to base / PRs to base / PRs to integration write the preset fields in one save. Base branch and release defined in are never filled by a preset. A hand-edit clears the model id so a stale preset name cannot survive; the picker then says Custom, not Unmeasured. Saving a model files a tracker checklist on this project; GET gitops.plan names it. Graphban still does not run git." },
      { num: 3, h: "The sitting", b: "Updates is this box's cut. Here, pick PRs to base, type the branch, optionally point Release defined in at the runbook — a path or URL, not a product CalVer (unmeasured is not docs/release.md). Graphban does not fetch it. Tracker then holds the graduation checklist (observe remote → first tagged cut). get_context returns the contract fields including that locator — not the model name and not whether this box is current." },
      { num: 4, h: "Grey when linked", b: "A linked box shows the org's live values, filled but not editable. control.message is the banner. Local columns are was, never the form value." },
      { num: 5, h: "Patterns, not globs", b: "Branch and PR patterns may insert {item_id} {tag} {slug} {version} {date}. base_branch is a literal. Globs are rejected." },
    ],
    related: [
      { label: "Updates", to: settingsPath("deployment/updates") },
      { label: "Tracker", to: "/tracker" },
      { label: "Cloud / Sync", to: settingsPath("deployment/sync") },
    ],
  },

  "/profile": {
    badge: "PROFILE",
    title: "Profile",
    tagline: "Your account and project access.",
    sections: [
      { num: 1, h: "Account", b: "Your name, handle, and email." },
      { num: 2, h: "Project access", b: "Every project you belong to, with your role and access level." },
    ],
    related: [{ label: "Settings", to: "/settings" }],
  },

  "org-gitops": {
    badge: "GITOPS",
    title: "Org gitops",
    tagline: "House process and per-project overlay.",
    sections: [
      { num: 1, h: "House process", b: "The org default: a named model writes the preset fields, or set them by hand. Base branch and release defined in are never filled by a preset. Unset is unmeasured — not main, not docs/release.md, and not no requirements. A hand-edit clears the model id; the picker then says Custom, not Unmeasured. Saving the house model does not file a graduation checklist on every project." },
      { num: 2, h: "Project overlay", b: "Empty overlay inherits the house value. Sparse fields are inheritance. Clearing a field returns it to inherit. Overlay rows come from every org project, not the readable subset." },
      { num: 3, h: "Linked boxes", b: "A linked self-host box reads this contract live. Gitops is not a property of a link key, so it is not on Deployments cards." },
    ],
  },

  "org-overview": {
    badge: "ORGANIZATION",
    title: "Organization",
    tagline: "How these projects are doing, together.",
    sections: [
      { num: 1, h: "A join, not a new write", b: "Every number already exists in a table. A brand-new org and an org whose boxes have all stopped pushing are different facts — never and live are separate words." },
      { num: 2, h: "Never-synced is shown", b: "A project that has never pushed is on this page, not filtered out. Omitting it would shrink the org and hide the one that needs attention." },
    ],
  },

  "org-galaxy": {
    badge: "GALAXY",
    title: "Galaxy",
    tagline: "How this org's repos relate — every edge names the file that proves it.",
    sections: [
      { num: 1, h: "Evidence, not similarity", b: "Two repos that both describe authentication are not related. Two repos where one's lockfile names the other are. Hover an edge for the file." },
      { num: 2, h: "Stale is a filter, not absence", b: "Hiding stale edges does not mean the org has no internal dependencies. The empty state keys off what the org HAS, not what is currently drawn." },
    ],
  },

  "org-users": {
    badge: "USERS",
    title: "Users & access",
    tagline: "Memberships and invites — the seat meter runs ahead of the table.",
    sections: [
      { num: 1, h: "Seats include pending invites", b: "The meter counts memberships plus outstanding invites, so it looks ahead of the table. Showing the number without that is why the table looks like a lie." },
    ],
  },

  "org-roles": {
    badge: "ROLES",
    title: "Roles & permissions",
    tagline: "People-roles. Fleet roles are a seat, not a credential.",
    sections: [
      { num: 1, h: "Not a missing page", b: "If you can open this, the speculative flag is on. A URL that 404s when the flag is off is the decision, not an oversight." },
      { num: 2, h: "People, not fleet", b: "These are roles for people. Agent roles — planner, worker, reviewer — are a seat on Fleet, enforced at call time — not a credential. A change on this screen cannot grant or remove one. That word is an LLM provider row." },
    ],
    related: [
      { label: "Users & access", to: adminPath("users") },
      { label: "Fleet", to: "/fleet" },
    ],
  },

  "org-teams": {
    badge: "TEAMS",
    title: "Teams",
    tagline: "A grant writes real project memberships. It is not resolved later.",
    sections: [
      { num: 1, h: "Grants materialize", b: "Setting a grant writes memberships now. Revoking one removes them. This is not a filter applied when someone asks for a page." },
    ],
  },

  "org-deployments": {
    badge: "DEPLOYMENTS",
    title: "Deployments",
    tagline: "The box pushes. The cloud never reaches in.",
    sections: [
      { num: 1, h: "Mint a link key in this org", b: "Settings → Sync / Link (or API keys → Link key). Scope sync, pinned to one project. The key's name is the deployment's identity here. Nothing is linked until that key is pasted on the box." },
      { num: 2, h: "Address as text, then a link", b: "The console cannot test reachability — that is the viewer's network. A dead Open button is worse than showing http://ubuntu-srv:8080 first." },
    ],
  },

  "org-integrations": {
    badge: "ORG INTEGRATIONS",
    title: "Org integrations",
    tagline: "Connector × project. There is no org-level GitHub toggle.",
    sections: [
      { num: 1, h: "PlatformConfig is per project", b: "GitHub and Drive are real. Jira, Linear, Confluence, Trello are not on this row — they carry their own chip so a missing connection is not drawn as off." },
    ],
  },

  "org-billing": {
    badge: "BILLING",
    title: "Billing",
    tagline: "Limits, and Checkout when self-serve is on.",
    sections: [
      { num: 1, h: "Two modes", b: "Unset Stripe keys keep operator-assigned plans — that is not a broken billing page. When self-serve is on, Checkout upgrades Pro/Team and the portal manages the subscription. Enterprise is operator-assigned even then — Checkout is Pro/Team only, not a missing button; portal still if a customer exists. There is still no invoice list and no usage chart — OrgUsage holds one row per period. An operator can still assign a plan by hand." },
    ],
  },

  "org-branding": {
    badge: "BRANDING",
    title: "Branding",
    tagline: "Speculative — gated on the route, not only the nav.",
    sections: [
      { num: 1, h: "Not a missing page", b: "If you can open this, the speculative flag is on. A URL that 404s when the flag is off is the decision, not an oversight." },
    ],
  },

  "project-home": {
    badge: "PROJECT",
    title: "Project home",
    tagline: "The existing app, from this project's place in the org.",
    sections: [
      { num: 1, h: "A landing pad", b: "No new backend. The strip is this project's galaxy edges: what it depends on, and what depends on it. Only one of those is visible from inside the repo." },
    ],
  },

  "operator": {
    badge: "OPERATOR",
    title: "Operator",
    tagline: "Every tenant on this deployment, at a glance.",
    sections: [
      { num: 1, h: "The same four counters", b: "Numbers here are summed from what an org sees on its own billing screen. A figure only the operator can see is a figure nobody can reconcile." },
      { num: 2, h: "Orgs, users, licensing", b: "Issue a platform invite from Licensing. The first org appears here when it is redeemed — an empty list is a fresh install, not a fault." },
    ],
  },
};

/** Global shortcuts shown on every page (these are actually wired). */
export const GLOBAL_SHORTCUTS: DocShortcut[] = [
  { k: "?", d: "Open / close this help" },
  { k: "Esc", d: "Close this panel" },
];

/** Strip hosted `/p/:tag` so the same overlay keys work on both planes. */
function hostedView(pathname: string): { path: string; projectRoot: boolean } {
  const m = /^\/p\/[^/]+(\/.*)?$/.exec(pathname);
  if (!m) return { path: pathname, projectRoot: false };
  const rest = m[1];
  if (!rest || rest === "/") return { path: "/", projectRoot: true };
  return { path: rest, projectRoot: false };
}

export function docFor(pathname: string): DocEntry {
  const { path, projectRoot } = hostedView(pathname);
  if (projectRoot) return CONTENT["project-home"];

  if (/^\/prds\/[^/]+$/.test(path)) return CONTENT["prd-editor"];
  if (/^\/lessons(?:\/[^/]+)?$/.test(path)) return CONTENT["/lessons"];
  if (path === "/home") return CONTENT["/home"];
  if (path === "/dashboard") return CONTENT["/dashboard"];

  if (path.startsWith("/admin")) return CONTENT["operator"];

  if (path.startsWith(adminPath("gitops"))) return CONTENT["org-gitops"];
  if (path.startsWith(adminPath("users/roles"))) return CONTENT["org-roles"];
  if (path.startsWith(adminPath("users"))) return CONTENT["org-users"];
  if (path.startsWith(adminPath("teams"))) return CONTENT["org-teams"];
  if (path.startsWith(adminPath("deployments"))) return CONTENT["org-deployments"];
  if (path.startsWith(adminPath("integrations"))) return CONTENT["org-integrations"];
  if (path.startsWith(adminPath("billing"))) return CONTENT["org-billing"];
  if (path.startsWith(adminPath("branding"))) return CONTENT["org-branding"];
  if (path === adminPath() || path === `${adminPath()}/`) return CONTENT["org-users"];
  if (path === orgPath("galaxy") || path.startsWith(`${orgPath("galaxy")}/`)) {
    return CONTENT["org-galaxy"];
  }
  if (path === ORG_BASE || path === `${ORG_BASE}/`) return CONTENT["org-overview"];

  // Settings is path-per-item (GRPH-P28 D3). Match the page before the /settings catch-all
  // or API keys, Sync, Updates, Gitops all open as AI Providers.
  if (path.startsWith(settingsPath("deployment/updates"))) return CONTENT["/settings/deployment/updates"];
  if (path.startsWith(settingsPath("deployment/gitops"))) return CONTENT["/settings/deployment/gitops"];
  if (path.startsWith(settingsPath("deployment/sync"))) return CONTENT["/settings/deployment/sync"];
  if (path.startsWith(settingsPath("deployment/providers"))) return CONTENT["/settings/project/providers"];
  if (path.startsWith(settingsPath("project/mcp"))) return CONTENT["/mcp-tools"];
  if (path.startsWith(settingsPath("project/feedback-kit"))) return CONTENT["/feedback-kit"];
  if (path.startsWith(settingsPath("project/api-keys"))) return CONTENT["/settings/project/api-keys"];
  if (path.startsWith(settingsPath("project/providers"))) return CONTENT["/settings/project/providers"];
  if (path.startsWith(settingsPath("project/integrations"))) return CONTENT["/settings/project/integrations"];
  if (path.startsWith(settingsPath("project/members"))) return CONTENT["/settings/project/members"];
  if (path === settingsPath("project") || path === `${settingsPath("project")}/`) {
    return CONTENT["/settings/project"];
  }
  if (path.startsWith(settingsPath("account"))) return CONTENT["/settings/account"];
  if (path.startsWith("/settings")) return CONTENT["/settings"];

  return CONTENT[path] ?? CONTENT.default;
}
