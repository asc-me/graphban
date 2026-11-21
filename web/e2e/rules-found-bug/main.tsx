import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { ProjectProvider } from "@/features/ProjectContext";
import { RulesView } from "@/features/rules/RulesView";
import "@/index.css";

/**
 * GRPH-1703: the real Rules page on its Build stage, with the app's CSS and a stubbed `/api`,
 * so `e2e/rules-found-bug.play.ts` can capture `build.found_bug` in each state.
 * `?state=on` serves the rule on in `file_ticket`; `?state=off` serves it off (the default
 * `fix_in_build` is in force); `?state=absent` serves a pack with no `build.found_bug` template.
 */
const state = new URLSearchParams(location.search).get("state") ?? "on";
const flow = new URLSearchParams(location.search).get("flow");
const PROJECT = { id: "p1", name: "Core Platform", tag: "CP" };
const RULE = {
  id: "build.found_bug", stage: "build", title: "What a builder does with a bug it finds",
  summary: "A builder that finds a bug outside its item either fixes it in the build or files it as its own item.",
  enforcement: "checked", channel: null, enforcer: { kind: "doc", ref: "docs/rules.md", url: "" }, truth: "db",
  scope: "project", models: ["push_to_base", "prs_to_base", "prs_to_integration"], control: "toggle",
  value: state === "on", source: "project", params: { value: { mode: "file_ticket" }, source: "project" },
  locked: false, used: state === "on", label: null, skills: null,
};
const f = (value: unknown) => ({ value, source: "project" });
// GRPH-1728: `?flow=p74` serves the strip the owner saw on p74-walk — Build carries the longest real
// tally (9 enforced · 3 checked · 1 advisory · 1 unmeasured) — and a stage at every level.
const mk = (stage: string, n: number, enforcement: string, extra: Record<string, unknown> = {}) =>
  Array.from({ length: n }, (_, i) => ({
    ...RULE, id: `${stage}.${enforcement}_${i}`, stage, title: `${enforcement} rule ${i + 1}`,
    enforcement, used: true, control: "fixed", params: null, ...extra,
  }));
const P74 = [
  { id: "plan", title: "Plan", summary: "", state: "enforced", rules: mk("plan", 2, "enforced") },
  { id: "build", title: "Build", summary: "What a builder must do while building.", state: "advisory", rules: [
    { ...RULE, used: true }, ...mk("build", 9, "enforced"), ...mk("build", 2, "checked"), ...mk("build", 1, "advisory"),
    ...mk("build", 1, "checked", { unmeasured_reason: "refused by the pack loader" }),
  ] },
  { id: "review", title: "Review", summary: "", state: "advisory", rules: [
    ...mk("review", 12, "enforced"), ...mk("review", 10, "checked"), ...mk("review", 11, "advisory"),
    ...mk("review", 13, "checked", { unmeasured_reason: "refused by the pack loader" }), ...mk("review", 14, "checked", { used: false }),
  ] },
  { id: "ship", title: "Ship", summary: "", state: "checked", rules: mk("ship", 3, "checked") },
];
function answer(path: string): unknown {
  if (path === "/api/rules/flow") return {
    version: "1.0.0", model: "prs_to_base", control: { state: "local", writable: true, message: "", org_name: "" }, edges: [],
    stages: flow === "p74" ? P74 : [{ id: "build", title: "Build", summary: "What a builder must do while building.", state: "checked", rules: state === "absent" ? [] : [RULE] }],
  };
  if (path === "/api/rules/toggles") return { library_loaded: true, toggles: [] };
  if (path.endsWith("/gitops")) return {
    project_id: "p1", org_id: null,
    fields: { base_branch: f("main"), no_push_to_base: f(null), branch_name_pattern: f(null), pr_title_pattern: f(null), reviewer_bar: f(null) },
    control: { state: "local", writable: true, message: "" }, was: null,
    version_from: f(null), release_defined_in: f(null), model: f(null), plan: null, projects: [],
  };
  if (path === "/api/config") return { hosted_mode: false };
  if (path === "/api/sync/status") return { linked: false, org: "" };
  if (path === "/api/events") return { results: [], total: 0 };
  if (path === "/api/rules/refusals/unmapped") return { groups: [] };
  if (path === "/api/rules/recommended") return { suggestions: [], sources: [] };
  if (path.startsWith("/api/projects")) return [PROJECT];
  return null;
}
window.fetch = async (input: RequestInfo | URL) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
  // Anything the page reads that this harness does not serve is a 404: the page shows it as unmeasured.
  const body = answer(url.pathname);
  return new Response(JSON.stringify(body ?? { detail: "not served by the harness" }), { status: body === null ? 404 : 200, headers: { "Content-Type": "application/json" } });
};
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={["/p/CP/rules?stage=build"]}>
      <ProjectProvider><RulesView /></ProjectProvider>
    </MemoryRouter>
  </QueryClientProvider>,
);
