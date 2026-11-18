import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { ProjectProvider } from "@/features/ProjectContext";
import { TriageView } from "@/features/triage/TriageView";
import "@/index.css";

/**
 * GRPH-1700: the real Triage page with the app's CSS and a stubbed `/api`, so
 * `e2e/triage-bug-line.play.ts` can capture the bug pane in each state.
 * `?state=line` serves a maintenance line; `?state=none` serves none (until POST creates it).
 * Accepting returns item CP-412; the test clicks accept to reach "accepted and unready".
 */
const state = new URLSearchParams(location.search).get("state") ?? "line";
const PROJECT = {
  id: "p1", name: "Core Platform", tag: "CP", accent: "#c6f24e", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "", credential_id: null,
  model_override: "", memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};
const LINE = { id: "line1", title: "Maintenance" };
let line: typeof LINE | null = state === "none" ? null : LINE;
let accepted = false;
const BUG = {
  id: "CP-R31", kind: "bug", title: "Export to CSV drops the last row", age_seconds: 5400,
  paths: [], by: "dana", votes: 4, source: "Public form",
  body: "Exporting a 120-row board gives a CSV with 119 rows. The last item is always missing.",
  duplicate: null, suggested_prd: { id: "CP-P12", score: 0.58 },
};
const FEATURE = {
  id: "CP-R32", kind: "feature", title: "Dark mode for the public board", age_seconds: 9000,
  paths: [], by: "sam", votes: 2, source: "In-app widget", body: "", duplicate: null, suggested_prd: null,
};
function answer(method: string, path: string): unknown {
  if (path === "/api/projects/p1/line") {
    if (method === "POST") { line = LINE; return LINE; }
    return line ?? "null";
  }
  if (path === "/api/projects/p1/triage") {
    const rows = accepted ? [FEATURE] : [BUG, FEATURE];
    return { rows, counts: { all: rows.length, bugs: accepted ? 0 : 1, features: 1, blockers: 0, decisions: 0, answered: 0 }, failed_sources: [] };
  }
  if (path === "/api/requests/CP-R31/accept") {
    accepted = true;
    return { request: { id: "CP-R31" }, item: { id: "CP-412" }, action_id: "act1" };
  }
  if (path.endsWith("/comments")) return [];
  if (path === "/api/config") return { hosted_mode: false };
  if (path.startsWith("/api/projects")) return [PROJECT];
  return null;
}
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
  const body = answer((init?.method ?? "GET").toUpperCase(), url.pathname);
  if (body === "null") return new Response("null", { status: 200, headers: { "Content-Type": "application/json" } });
  return new Response(JSON.stringify(body ?? { detail: "not served by the harness" }), { status: body === null ? 404 : 200, headers: { "Content-Type": "application/json" } });
};
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={["/p/CP/triage"]}>
      <ProjectProvider><TriageView /></ProjectProvider>
    </MemoryRouter>
  </QueryClientProvider>,
);
