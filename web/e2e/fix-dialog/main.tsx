import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { ProjectProvider } from "@/features/ProjectContext";
import { NewItemDialog } from "@/features/tracker/NewItemDialog";
import "@/index.css";

/**
 * GRPH-1699: the real New item dialog with the app's CSS and a stubbed `/api`, so
 * `e2e/fix-dialog-states.play.ts` can walk each Fix Dialog state in a real browser and
 * capture it. `?line=1` serves a maintenance line; without it the project has none and
 * POST /line creates one. Every item create is refused with a 422, for the refusal state.
 */

const q = new URLSearchParams(location.search);
let line: unknown = q.get("line") === "1" ? { id: "L1", title: "Maintenance — Core Platform" } : null;
const PROJECT = { id: "p1", name: "Core Platform", tag: "CP" };
function answer(method: string, path: string): [number, unknown] {
  if (path === "/api/projects/p1/line") {
    if (method === "POST") { line = { id: "L1", title: "Maintenance — Core Platform" }; return [200, line]; }
    return [200, line];
  }
  if (path.startsWith("/api/projects")) return [200, [PROJECT]];
  if (path.startsWith("/api/prds")) return [200, [{ id: "CP-P4", title: "Billing v2" }]];
  if (path.startsWith("/api/items") && method === "POST")
    return [422, { detail: "a fix filed on the line needs acceptance clauses and at least one touchpoint to be claimable by an agent; a person may file it unready — add them or file again" }];
  if (path.startsWith("/api/items")) return [200, [
    { id: "CP-41", title: "Invoice PDF export", prd_id: "CP-P4" },
    { id: "CP-42", title: "Proration on plan change", prd_id: "CP-P4" },
  ]];
  return [200, []];
}
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
  const [status, body] = answer((init?.method ?? "GET").toUpperCase(), url.pathname);
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
};
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={["/p/CP/tracker"]}>
      <ProjectProvider><main className="p-6"><NewItemDialog /></main></ProjectProvider>
    </MemoryRouter>
  </QueryClientProvider>,
);
