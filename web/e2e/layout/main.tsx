import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";

import { ProjectProvider } from "@/features/ProjectContext";
import { ItemDetailPanel } from "@/features/tracker/ItemDetailPanel";
import type { Item } from "@/lib/types";
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

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={["/p/CP/tracker"]}>
      <ProjectProvider>
        <ItemDetailPanel item={item} open onClose={() => {}} onStatus={() => {}} />
      </ProjectProvider>
    </MemoryRouter>
  </QueryClientProvider>,
);
