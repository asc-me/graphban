import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { LeftNav, SECTIONS } from "@/components/shell/LeftNav";
import { ProjectProvider } from "@/features/ProjectContext";

/**
 * GRPH-1142 — the current section's pages sit inline; every other section is one row (or
 * rail icon) that opens a 264px flyout beside the sidebar. Opening never navigates.
 */

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

vi.mock("@/lib/api", () => ({
  hasSession: () => true,
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({
      items: 41, items_in_progress: 3, requests: 7, review: 5, triage_new: 4, lessons_published: 6,
      items_open: 41, requests_untriaged: 7, code_nodes: 2184, mcp_tools: 24, fleet_agents: 3, outposts: 2, live_agents: 2,
    })),
    orgs: vi.fn(async () => [{ id: "org1", name: "Acme", plan: "pro", role: "owner" }]),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
  },
}));

function wrap(ui: ReactNode, path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="*" element={ui} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}


const iconOf = (el: Element) => el.querySelector("svg")?.getAttribute("class") ?? "";

describe("GRPH-1173 sidebar icons", () => {
  it("renders the Dashboard row without an icon", async () => {
    wrap(<LeftNav />, "/p/CORE/tracker");
    const nav = within(document.querySelector<HTMLElement>('[data-layer="expanded"]')!);
    const row = (await nav.findByRole("link", { name: "Dashboard" })) as HTMLElement;
    expect(row.querySelector("svg")).toBeNull();
  });

  it("gives the collapsed rail's Dashboard the design's grid icon, not a letter", async () => {
    wrap(<LeftNav />, "/p/CORE/tracker");
    const rail = within(document.querySelector<HTMLElement>('[data-layer="rail"]')!);
    const link = (await rail.findByRole("link", { name: "Dashboard", hidden: true })) as HTMLElement;
    expect(iconOf(link)).toContain("lucide-layout-grid");
    expect(link.textContent?.trim()).toBe("");
  });

  it("gives each section and page the design's icon", () => {
    const want: Record<string, string> = {
      Plan: "clipboard-check", Build: "hammer", Observe: "activity",
      "Code graph": "workflow", Links: "link", Stack: "layers", Live: "radio",
      Activity: "history", Memory: "brain", Lessons: "book-open", Rules: "scale",
    };
    const seen: Record<string, string> = {};
    for (const s of SECTIONS) {
      seen[s.label] = (s.icon as { type: { displayName?: string } }).type.displayName ?? "";
      for (const p of s.pages) seen[p.label] = (p.icon as { type: { displayName?: string } }).type.displayName ?? "";
    }
    const names: Record<string, string> = {
      ClipboardCheck: "clipboard-check", Hammer: "hammer", Radio: "radio", Workflow: "workflow",
      Link: "link", Layers: "layers", History: "history", Brain: "brain", BookOpen: "book-open",
      Activity: "activity", Scale: "scale",
    };
    for (const [label, icon] of Object.entries(want)) expect(names[seen[label]], label).toBe(icon);
    expect(iconOf(document.body)).toBe("");
  });

  it("gives Usage a chart-line and Feedback Kit a message-square, distinct from PRDs", async () => {
    wrap(<LeftNav />, "/p/CORE/tracker");
    const nav = within(document.querySelector<HTMLElement>('[data-layer="expanded"]')!);
    const usage = (await nav.findByRole("link", { name: "Usage" })) as HTMLElement;
    const kit = nav.getByRole("link", { name: "Feedback Kit" }) as HTMLElement;
    expect(iconOf(usage)).toContain("lucide-chart-line");
    expect(iconOf(kit)).toContain("lucide-message-square");
    expect(iconOf(usage)).not.toContain("lucide-chart-column");
  });
  it("GRPH-1208 sizes the project card 42px with a mono 9px subline", async () => {
    wrap(<LeftNav />, "/tracker");
    const nav = within(document.querySelector<HTMLElement>('[data-layer="expanded"]')!);
    const card = await nav.findByRole("button", { name: "Switch project" });
    expect(card.className).toContain("h-[42px]");
    const sub = card.querySelector("span.block.font-mono");
    expect(sub?.className).toContain("text-[9px]");
    expect(sub?.className).toContain("tracking-[0.4px]");
  });

  it("GRPH-1208 shows a CLOUD pill and no building icon on the unlinked Organization row", async () => {
    wrap(<LeftNav />, "/tracker");
    const nav = within(document.querySelector<HTMLElement>('[data-layer="expanded"]')!);
    const pill = await nav.findByText("CLOUD");
    const row = pill.closest("button") as HTMLElement;
    expect(row).toHaveTextContent("Organization");
    expect(row.querySelector(".lucide-building-2")).toBeNull();
    expect(row.querySelector("svg")).toBeNull();
  });

  it("GRPH-1208 draws the rail project tile at 30px", async () => {
    wrap(<LeftNav />, "/tracker");
    const rail = within(document.querySelector<HTMLElement>('[data-layer="rail"]')!);
    const btn = await rail.findByRole("button", { name: "Switch project", hidden: true });
    expect(btn.querySelector("span")?.className).toContain("h-[30px]");
  });

  it("GRPH-1208 orders Build and Observe pages as the design does", () => {
    const order = (key: string) => SECTIONS.find((s) => s.key === key)!.pages.map((p) => p.label);
    expect(order("build")).toEqual(["Links", "Code graph", "Stack", "MCP Tools", "Rules", "Fleet", "Outposts"]);
    expect(order("observe")).toEqual(["Memory", "Lessons", "Activity", "Live", "Harness"]);
  });
});
