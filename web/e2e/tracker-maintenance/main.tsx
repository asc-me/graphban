import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { ProjectProvider } from "@/features/ProjectContext";
import { TRACKER_LAYOUT_KEY, TrackerView } from "@/features/tracker/TrackerView";
import "@/index.css";

/**
 * GRPH-1701: the real tracker page with the app's CSS and a stubbed `/api`, so
 * `e2e/tracker-maintenance.play.ts` can capture the Maintenance filter in each state.
 * `?line=none` serves a project with no maintenance line; `?line=empty` a line with nothing on
 * it; `?line=populated` a line carrying a SECURITY fix, an unready fix and a fix that blocks a
 * PRD item. `?layout=board|list` picks the tracker layout. Unscoped fixes count is 2.
 */
const q = new URLSearchParams(location.search);
const mode = q.get("line") ?? "populated";
localStorage.setItem(TRACKER_LAYOUT_KEY, q.get("layout") ?? "list");
localStorage.setItem("gb_views_tracker", JSON.stringify({ saved: [], defaultId: "all" }));

const PROJECT = {
  id: "p1", name: "Core Platform", tag: "CP", accent: "#a78bfa", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
};
const LINE = { id: "CP-L1", title: "Maintenance — Core Platform" };
let line: unknown = mode === "none" ? null : LINE;
const base = {
  project_id: "p1", description: "", status: "next", tags: [], touchpoints: ["api/billing.py"], effort: 1, sort_order: 0,
  blocker: "", bounce_reason: "", date: "", reporter: { name: "Alex", handle: "alex", avatar: "#a78bfa" }, pr: null,
  github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low",
  reach: "repo", created_at: "2026-11-17T10:00:00Z", updated_at: "2026-11-18T09:00:00Z", done_at: null,
  kind: "feature", line_id: null, severity: "",
};
const ACCEPT = "Fix it.\n\n## Acceptance\n- the bug no longer reproduces";
const it = (o: Record<string, unknown>) => ({ ...base, ...o });
const prdItems = [
  it({ id: "CP-41", title: "Invoice PDF export", prd_id: "CP-P4", status: "blocked", sort_order: 1 }),
  it({ id: "CP-42", title: "Proration on plan change", prd_id: "CP-P4", status: "in_progress", sort_order: 2 }),
];
const lineItems = mode === "populated" ? [
  it({ id: "CP-50", title: "Session token not rotated on password reset", kind: "fix", severity: "security", line_id: "CP-L1", description: ACCEPT, sort_order: 3 }),
  it({ id: "CP-51", title: "Retry banner flickers on slow networks", kind: "fix", line_id: "CP-L1", touchpoints: [], sort_order: 4 }),
  it({ id: "CP-52", title: "Tax rounding off by a cent on EUR invoices", kind: "fix", line_id: "CP-L1", status: "in_progress", description: ACCEPT, sort_order: 5 }),
] : [];
const items = [...prdItems, ...lineItems];
// CP-41 depends on CP-52: the fix blocks the invoice export.
const links = mode === "populated" ? [{ id: 1, a: "CP-41", b: "CP-52", type: "dependency", reason: "blocked by a fix" }] : [];

function answer(method: string, path: string, search: URLSearchParams): [number, unknown] {
  if (path === "/api/projects/p1/line/unscoped_fixes") return [200, { ids: ["CP-60", "CP-61"], count: 2 }];
  if (path === "/api/projects/p1/line") {
    if (method === "POST") { line = LINE; return [200, line]; }
    return [200, line];
  }
  if (path.startsWith("/api/projects")) return [200, [PROJECT]];
  if (path === "/api/items") return [200, search.get("fields") === "full" ? items : items.map(({ description: _d, ...r }) => r)];
  if (path === "/api/links") return [200, links];
  if (path === "/api/auth/me") return [200, { name: "Alex" }];
  if (path === "/api/config") return [200, { hosted_mode: false }];
  return [200, []];
}
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
  const [status, body] = answer((init?.method ?? "GET").toUpperCase(), url.pathname, url.searchParams);
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
};
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={["/p/CP/tracker"]}>
      <ProjectProvider>
        <Routes>
          <Route element={<Outlet context={""} />}>
            <Route path="/p/CP/tracker" element={<TrackerView />} />
          </Route>
        </Routes>
      </ProjectProvider>
    </MemoryRouter>
  </QueryClientProvider>,
);
