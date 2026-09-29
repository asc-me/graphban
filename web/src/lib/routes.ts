/**
 * URL construction for the two-level hierarchy (PRD-21 D1).
 *
 * Two rules live here rather than in the components:
 *
 * 1. **Nothing hardcodes `/org`.** Every org-plane path is built from {@link ORG_BASE}.
 *    Per-org hostnames (`acme.graphban.dev`) are not built yet, but when they are, the
 *    base becomes `""` and every link follows for free. Honouring it now costs nothing;
 *    skipping it costs every org link later.
 * 2. **The project segment is the tag, used verbatim.** `Project.tag` is 2–4 uppercase
 *    characters (`backend/app/tagging.py:TAG_RE`) and is already how item keys render,
 *    so `/p/GRPH/code` and `GRPH-406` agree. There is no canonical-lowercase rewrite,
 *    because that would invent a second representation of an identifier that has one.
 */

/** The org plane's root. `""` when an org is served from its own host. */
export const ORG_BASE: string = "/org";

/** Mirrors `tagging.TAG_RE`. Case-insensitive here because a URL is typed by humans. */
export const TAG_RE = /^[A-Za-z][A-Za-z0-9]{1,3}$/;

/** `/org`, `/org/projects`, … — always through here. */
export function orgPath(sub = ""): string {
  const tail = sub.replace(/^\/+/, "");
  const base = ORG_BASE || "/";
  if (!tail) return base;
  return base === "/" ? `/${tail}` : `${base}/${tail}`;
}

/** `/p/GRPH/tracker`. `tag` is used verbatim — never lowercased. */
export function projectPath(tag: string, sub = ""): string {
  const tail = sub.replace(/^\/+/, "");
  return tail ? `/p/${tag}/${tail}` : `/p/${tag}`;
}

/** The org-admin section. Org-scoped, so it hangs off the org base, not a project. */
export function adminPath(sub = ""): string {
  return orgPath(sub ? `admin/${sub.replace(/^\/+/, "")}` : "admin");
}

/** `/settings`, `/settings/project/mcp` — Settings is path-per-item (GRPH-P28 D3), hosted and self-host. */
export function settingsPath(sub = ""): string {
  const tail = sub.replace(/^\/+/, "");
  return tail ? `/settings/${tail}` : "/settings";
}

/** A cloud console URL we will actually open. Missing/junk is not a URL (GRPH-P28 D5). */
export function usableHttpUrl(raw: string | null | undefined): string | null {
  const s = (raw ?? "").trim();
  if (!s) return null;
  try {
    const u = new URL(s);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.toString();
  } catch {
    return null;
  }
}

/**
 * The tag in a `/p/:tag/...` path, or null.
 *
 * Read from the pathname rather than `useParams` so the provider that supplies project
 * context can sit above the routes that declare `:tag` — it is still the route, and
 * still the only source. Returns null for every other path, including the org plane.
 */
export function tagFromPath(pathname: string): string | null {
  const m = /^\/p\/([^/]+)/.exec(pathname);
  if (!m) return null;
  const tag = decodeURIComponent(m[1]);
  return TAG_RE.test(tag) ? tag : null;
}

/** The view segment after the tag: `/p/GRPH/code/x` → `code/x`. Empty at the root. */
export function viewFromPath(pathname: string): string {
  const m = /^\/p\/[^/]+\/?(.*)$/.exec(pathname);
  return m ? m[1] : "";
}

/** Section · page for the project bar (GRPH-953). Never title-cases the whole path. */
export type Place = { section: string; page: string };

const VIEW_PLACE: Record<string, Place> = {
  tracker: { section: "Plan", page: "Tracker" },
  requests: { section: "Plan", page: "Requests" },
  triage: { section: "Plan", page: "Triage" },
  dashboard: { section: "Plan", page: "Dashboard" },
  prds: { section: "Plan", page: "PRDs" },
  roadmap: { section: "Plan", page: "Roadmap" },
  links: { section: "Build", page: "Links" },
  code: { section: "Build", page: "Code graph" },
  "mcp-tools": { section: "Build", page: "MCP Tools" },
  "fleet.v2": { section: "Build", page: "Fleet.v2" },
  "fleet.v1": { section: "Build", page: "Fleet.v1" },
  fleet: { section: "Build", page: "Fleet" },
  outposts: { section: "Build", page: "Outposts" },
  "memory-review": { section: "Observe", page: "Memory review" },
  "memory-triage": { section: "Observe", page: "Memory triage" },
  lessons: { section: "Observe", page: "Lessons" },
  activity: { section: "Observe", page: "Activity" },
  live: { section: "Observe", page: "Live" },
  harness: { section: "Observe", page: "Harness" },
  "feedback-kit": { section: "Project", page: "Feedback Kit" },
  home: { section: "Home", page: "Home" },
  usage: { section: "Deployment", page: "Usage" },
};

const SETTINGS_PAGE: Record<string, string> = {
  providers: "AI providers",
  sync: "Cloud / Sync",
  gitops: "Gitops",
  updates: "Updates",
  "log-export": "Log export",
  "api-keys": "API keys",
  mcp: "MCP Tools",
  integrations: "Integrations",
  members: "Members",
  project: "Project",
  account: "Account",
  "feedback-kit": "Feedback Kit",
};

/**
 * The crumb after org / project. `/settings/project/mcp` is Settings · MCP Tools,
 * not "Settings/Project/Mcp". Unknown first segments return null rather than a
 * title-cased slug — a missing name is louder than a guessed one.
 */
export function placeFromPath(pathname: string): Place | null {
  const hosted = viewFromPath(pathname);
  const rest = hosted || (tagFromPath(pathname) ? "" : pathname.replace(/^\/+/, ""));
  if (!rest) return null;
  const segs = rest.split("/").filter(Boolean);
  const first = segs[0] ?? "";
  if (first === "settings") {
    const last = segs[segs.length - 1] ?? "settings";
    return { section: "Settings", page: SETTINGS_PAGE[last] ?? last };
  }
  return VIEW_PLACE[first] ?? null;
}

// ── last-used project ──────────────────────────────────────────────────────
// Moves off the module-level variable it used to share with the API client. This is a
// *hint* for resolving a flat path, never an input to a request: the route is what a
// write is scoped by, so a stale entry here can send you to the wrong page but can
// never write to the wrong project.
const LAST_TAG_KEY = "gb_last_project_tag";

export function rememberProjectTag(tag: string): void {
  try {
    localStorage.setItem(LAST_TAG_KEY, tag);
  } catch {
    // Private mode / disabled storage: the flat-path resolver falls back to the first
    // readable project, which is a worse guess but never a wrong one.
  }
}

export function lastProjectTag(): string | null {
  try {
    return localStorage.getItem(LAST_TAG_KEY);
  } catch {
    return null;
  }
}

/**
 * Drop org-plane state on a self-host boot.
 *
 * A self-host build has no way to serve an org, so an org key left behind by a hosted
 * session must not resurrect a context that cannot exist here (PRD-21 D1.2).
 */
export function clearOrgStateForSelfHost(): void {
  try {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith("gb_org_")) localStorage.removeItem(key);
    }
  } catch {
    // nothing to clear if storage is unavailable
  }
}
