import { adminPath } from "@/lib/routes";

/** Context-aware content for the inline docs reader, keyed by route.
 *  Distilled from the docs/ guides into the reader's structured format. */

import { settingsPath } from "@/lib/routes";

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
    ],
    related: [
      { label: "MCP Tools", to: "/mcp-tools" },
      { label: "Dashboard", to: "/dashboard" },
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
      { num: 1, h: "The surface", b: "56 live tools over a JSON-RPC endpoint (POST /api/mcp) — 34 core, shipped to every key, the rest by tier. Each card shows its params, description, and call count." },
      { num: 2, h: "Authenticate agents", b: "Create a scoped API key in Settings → API Keys, then call the endpoint with it. Every call is metered here." },
      { num: 3, h: "Where keys come from", b: "Two paths, one key shape. Mint one here in the UI, or provision the first one without a browser: `graphban init` runs once on a virgin instance and creates the operator, the project, and its first key — `--key-scope` and `--key-tiers` are the same axes as the mint dialog." },
      { num: 4, h: "Scope and tiers", b: "A Project-scoped key points the agent's writes at one project; a Global key is unbound, so calls pass `project_id` (or fall back to the default). The connect snippets follow the key: where the harness supports scoping (Claude Code, Grok CLI), a project key gets `--scope project` and a global key `--scope user`. Tiers (prd / codegraph / fleet / misc) decide which specialist tools the agent can SEE in its manifest — visibility, not permission: scopes and roles decide what may be called. Keys are shown once and stored only as a hash; a lost key is a new mint." },
    ],
    related: [{ label: "Settings", to: "/settings" }],
  },

  "/memory-review": {
    badge: "MEMORY REVIEW",
    title: "Memory review",
    tagline: "Approve agent-written memory before it's trusted.",
    sections: [
      { num: 1, h: "Candidates, not truth", b: "Memory an agent writes (via add_memory or auto-extracted on Done) enters as a candidate. It stays out of the default semantic search until you publish it here." },
      { num: 2, h: "Publish or reject", b: "Publish promotes a shard into the trusted retrieval path every future agent searches. Reject keeps it for provenance but never surfaces it. Both are recorded in Activity." },
      { num: 3, h: "Recurring lessons", b: "When the same correction shows up several times, it's grouped as a recurring lesson — publish it once as a principle and drop the duplicates in a single action." },
    ],
    related: [
      { label: "Lessons", to: "/lessons" },
      { label: "Activity", to: "/activity" },
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
      { num: 1, h: "AI Providers", b: "Switch the chat/extraction provider (stub / Ollama / Claude) live. Embeddings are a deploy-time setting." },
      { num: 2, h: "Integrations", b: "Connect GitHub/Drive config and copy the inbound issues webhook — opened GitHub issues become tracker items." },
      { num: 3, h: "Project, members, keys", b: "Edit project config and flags, review member roles, and create/revoke API keys (shown once). The mint dialog asks for scope — Project pins the agent's writes to the active project, Global lets the agent name a project per call — and which tool tiers the key advertises. The same axes exist for the first-run key as `graphban init --key-scope` / `--key-tiers`." },
    ],
    related: [{ label: "MCP Tools", to: "/mcp-tools" }],
  },

  "/settings/project/api-keys": {
    badge: "API KEYS",
    title: "API keys",
    tagline: "Who an agent is, what it can see, and what it may call.",
    sections: [
      { num: 1, h: "Three kinds", b: "An agent key talks to MCP — read and write on items, memory, and code. A sync credential pushes a code graph from a local box into exactly one project and nothing else. A gate key attests that work was checked so an item may reach done: for CI or a reviewer, never for the agent doing the work. Gate is minted with read+write+gate; gate alone cannot attest." },
      { num: 2, h: "Scope is the write target", b: "Project pins the agent's writes to the active project. Global is unbound — calls pass project_id or fall back to the default. Connect snippets follow the key: Claude Code and Grok CLI get --scope project or --scope user. Sync and gate always pin to one project." },
      { num: 3, h: "Advertisement is not permission", b: "An agent key ships the core tools. Tiers (PRDs, code-graph writes, fleet admin, occasional) opt in specialist tools because they cost manifest tokens every turn. A tool left out of tools/list is still callable; scopes and roles decide what may be called. Visibility is not the gate. The symptom of a missing tier is an agent that does not know a tool exists, never an error." },
      { num: 4, h: "Shown once", b: "The plaintext is shown once and stored as a hash. A lost key is a new mint. Two paths, one key shape: mint here, or graphban init --key-scope / --key-tiers on a virgin instance." },
    ],
    related: [
      { label: "MCP Tools", to: settingsPath("project/mcp") },
    ],
  },

  "/settings/deployment/updates": {
    badge: "UPDATES",
    title: "Updates",
    tagline: "Whether this box is on the published stable cut. Checking is not applying.",
    sections: [
      { num: 1, h: "Three states", b: "current, available, or unknown. Unknown is not current — a failed feed fetch or a placeholder 0.1.0 version must not look up to date." },
      { num: 2, h: "No Apply yet", b: "This page reports. Compose: scripts/deploy.sh <tag>. Native apply is a later slice. Hosted instances are updated by the operator." },
    ],
    related: [{ label: "Gitops", to: settingsPath("deployment/gitops") }],
  },

  "/settings/deployment/gitops": {
    badge: "GITOPS",
    title: "Gitops",
    tagline: "This project's delivery contract — unmeasured is not main.",
    sections: [
      { num: 1, h: "Unmeasured, not main", b: "Unset fields are unmeasured — not 'use main' and not 'no requirements'. Agents read the resolved contract from get_context." },
      { num: 2, h: "Grey when linked", b: "A linked box shows the org's live values, filled but not editable. control.message is the banner. Local columns are was, never the form value." },
      { num: 3, h: "Patterns, not globs", b: "Branch and PR patterns may insert {item_id} {tag} {slug} {version} {date}. base_branch is a literal. Globs are rejected." },
    ],
    related: [{ label: "Cloud / Sync", to: settingsPath("deployment/sync") }],
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
      { num: 1, h: "House process", b: "The org default: integration base, whether agents may push to it, branch/PR naming, reviewer bar. Unset is unmeasured — not main and not no requirements." },
      { num: 2, h: "Project overlay", b: "Empty overlay inherits the house value. Sparse fields are inheritance. Clearing a field returns it to inherit. Overlay rows come from every org project, not the readable subset." },
      { num: 3, h: "Linked boxes", b: "A linked self-host box reads this contract live. Gitops is not a property of a deployment credential, so it is not on Deployments cards." },
    ],
  },
};

/** Global shortcuts shown on every page (these are actually wired). */
export const GLOBAL_SHORTCUTS: DocShortcut[] = [
  { k: "?", d: "Open / close this help" },
  { k: "Esc", d: "Close this panel" },
];

export function docFor(pathname: string): DocEntry {
  if (/^\/prds\/[^/]+$/.test(pathname)) return CONTENT["prd-editor"];
  // Hosted prefix + list/detail. Exact map would drop /lessons/:id and /p/:tag/lessons to default.
  if (/^\/(?:p\/[^/]+\/)?lessons(?:\/[^/]+)?$/.test(pathname)) return CONTENT["/lessons"];
  if (pathname === "/home") return CONTENT["/dashboard"];
  if (pathname.startsWith(settingsPath("deployment/updates"))) return CONTENT["/settings/deployment/updates"];
  if (pathname.startsWith(settingsPath("deployment/gitops"))) return CONTENT["/settings/deployment/gitops"];
  if (pathname.startsWith(adminPath("gitops"))) return CONTENT["org-gitops"];
  if (pathname.startsWith(settingsPath("project/mcp"))) return CONTENT["/mcp-tools"];
  if (pathname.startsWith(settingsPath("project/feedback-kit"))) return CONTENT["/feedback-kit"];
  if (pathname.startsWith(settingsPath("project/api-keys"))) return CONTENT["/settings/project/api-keys"];
  if (pathname.startsWith("/settings")) return CONTENT["/settings"];
  return CONTENT[pathname] ?? CONTENT.default;
}
