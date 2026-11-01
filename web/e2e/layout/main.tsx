import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";

import { PerformanceGrid } from "@/features/harness/PerformanceGrid";
import { ProjectProvider } from "@/features/ProjectContext";
import { ItemDetailPanel } from "@/features/tracker/ItemDetailPanel";
import type { HarnessCell, HarnessReport, Item } from "@/lib/types";
import "@/index.css";

/**
 * Renders the real ItemDetailPanel with the app's CSS and a stubbed `/api`, so a
 * Playwright test can measure it. No backend: every request answers from here.
 *
 * The provider list is the GRPH-1172 case — long model names, and an unconfigured
 * provider whose label gains " (configure in Settings)". The widest <option> used to
 * set the select's width and push it past the panel's right edge.
 */
const PROJECT = { id: "p1", name: "Core Platform", tag: "CP" };
const PROVIDERS = [
  {
    id: "ollama",
    label: "Ollama",
    kind: "ollama",
    chat_model: "qwen2.5-coder",
    models: ["qwen2.5-coder", "qwen2.5-coder:32b-instruct-q8_0-with-a-very-long-tag-name"],
    configured: true,
    active: true,
  },
  {
    id: "anthropic",
    label: "Anthropic",
    kind: "anthropic",
    chat_model: "claude-opus-5-5",
    models: ["claude-opus-5-5", "claude-sonnet-5-5-extended-context-preview-2026-10-29"],
    configured: true,
    active: false,
  },
  {
    id: "openai-compatible",
    label: "OpenAI-compatible endpoint",
    kind: "openai",
    chat_model: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    models: [],
    configured: false,
    active: false,
  },
];

function answer(path: string): unknown {
  if (path.startsWith("/api/projects")) return [PROJECT];
  if (path.startsWith("/api/assistant/providers")) return { providers: PROVIDERS };
  // The panel's own Activity list (GRPH-1199) reads an EventPage, not a bare array.
  if (path.startsWith("/api/events")) return { results: [], total: 0, limit: 50, offset: 0, has_more: false };
  return [];
}

window.fetch = async (input: RequestInfo | URL) => {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
  return new Response(JSON.stringify(answer(url.pathname)), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
};

const item = {
  id: "CP-19",
  project_id: PROJECT.id,
  title: "Public feedback form (embeddable)",
  description: "Standalone embeddable form that drops submissions into the triage queue.",
  status: "backlog",
  tags: ["frontend", "feedback"],
  touchpoints: [],
  effort: 2,
  evidence: [],
  fidelity: "low",
  reach: "repo",
  prd_id: null,
} as unknown as Item;

/**
 * `?view=harness-grid` (GRPH-1232): the harness × function grid with a wide catalog — every
 * leaf measured above the floor, so each gets a column — for the 1280px no-page-scroll check.
 */
const FAMILIES: Record<string, string[]> = {};
for (const f of ["A", "B", "C", "D", "E"]) FAMILIES[f] = [1, 2, 3, 4, 5].map((n) => `${f}${n}`);
FAMILIES.other = [];

function leaf(model: string, capability: string, finished: number, signed_off: number): HarnessCell {
  return {
    key: { vendor: "anthropic", model, binary_version: "2.1.0", capability, size_band: "M" },
    kind: "leaf", family: capability[0], label: capability,
    finished, signed_off, bounced: finished - signed_off, rate: signed_off / finished, below_floor: false,
    sampling: { first_choice: finished, fallback: 0, explicit: 0, unknown: 0, probe: 0 },
    skew: null, median_seconds: 600,
    cost: { comparable: true, reported: finished, finished, tokens_per_signed_off: 8100, tokens_in: 70000, tokens_out: 2900 },
    versions_seen: ["2.1.0"], is_current_version: true,
    series: [{ week: "2026-W43", finished, signed_off, rate: signed_off / finished, below_floor: false, median_seconds: 600 }],
  };
}

function harnessReport(): HarnessReport {
  const cells: HarnessCell[] = [];
  for (const model of ["claude-opus-5-5", "claude-sonnet-5-5-extended-context-preview-2026-10-29", "haiku"]) {
    for (const [family, leaves] of Object.entries(FAMILIES)) {
      if (family === "other") continue;
      const ls = leaves.map((cap, i) => leaf(model, cap, 10 + i, 6 + i));
      const finished = ls.reduce((s, l) => s + l.finished, 0);
      const signed_off = ls.reduce((s, l) => s + l.signed_off, 0);
      cells.push({ ...ls[0], kind: "family", family, label: "family rollup", key: { ...ls[0].key, capability: family }, finished, signed_off, rate: signed_off / finished, leaves: ls });
    }
  }
  return {
    project_id: PROJECT.id, window_days: 90, versions: "current", floor: 5, skew_share: 0.8,
    generated_at: new Date().toISOString(), cells, below_floor_count: 0,
    capability_set: {
      leaves: Object.values(FAMILIES).flat(),
      families: FAMILIES,
      labels: Object.fromEntries(Object.keys(FAMILIES).map((f) => [f, `Family ${f}`])),
    },
  };
}

const view = new URLSearchParams(location.search).get("view");

createRoot(document.getElementById("root")!).render(
  view === "harness-grid" ? (
    <main className="p-6">
      <PerformanceGrid data={harnessReport()} routing={[]} />
    </main>
  ) : (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/p/CP/tracker"]}>
        <ProjectProvider>
          <ItemDetailPanel item={item} open onClose={() => {}} onStatus={() => {}} />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>
  ),
);
