import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectTile, TILE_GLOW } from "@/components/ProjectTile";
import { ProjectBar } from "@/components/shell/ProjectBar";
import { ProjectHome } from "@/features/projecthome/ProjectHome";
import { ProjectProvider } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { projectFill } from "@/lib/projectFill";
import type { Project, ProjectPattern } from "@/lib/types";

/**
 * GRPH-1748 — one fill, drawn by ProjectSwitcher, ProjectBar and Project Home.
 * A caller that paints `accent` as a flat square, or that drops accent2 / pattern,
 * no longer matches `projectFill` and this file goes red.
 */

const base = {
  visibility: "private",
  description: "",
  share_global_memory: false,
  auto_extract: true,
  mcp_enabled: true,
  embed_model: "",
  credential_id: null,
  model_override: "",
  memory_auto_reject: true,
  memory_write_mode: "review",
  memory_llm_judge: false,
  agent_adjudication: false,
  allow_self_review: false,
};

function project(
  over: Partial<Project> & { id: string; tag: string; name: string; accent: string },
): Project {
  return { ...base, accent2: null, pattern: "none", ...over };
}

const core = project({
  id: "core",
  tag: "CORE",
  name: "Core",
  accent: "#c6f24e",
  accent2: "#67d4f0",
  pattern: "paisley",
});
const web = project({
  id: "web",
  tag: "WEB",
  name: "Web",
  accent: "#a78bfa",
  accent2: "#f47fb4",
  pattern: "stripes",
});
const docs = project({
  id: "docs",
  tag: "DOCS",
  name: "Docs",
  accent: "#efd76a",
  accent2: null,
  pattern: "none",
});

const projects = vi.hoisted(() => ({ list: [] as Project[] }));

vi.mock("@/lib/api", () => ({
  hasSession: () => true,
  api: {
    projects: vi.fn(async () => projects.list),
    projectSummaries: vi.fn(async () => []),
    orgs: vi.fn(async () => [{ id: "org1", name: "Acme", plan: "pro", role: "owner" }]),
    counts: vi.fn(async () => ({
      items: 1, items_in_progress: 0, requests: 0, review: 0, triage_new: 0, lessons_published: 0, live_agents: 0,
    })),
    dashboard: vi.fn(async () => ({
      items_total: 1, items_by_status: {}, effort_total: 0, done_count: 0, in_progress_count: 0,
      blocked_count: 0, requests_total: 0, requests_by_type: {}, requests_by_status: {},
      shard_count: 0, prd_count: 0, mcp_calls: 0, recent_items: [],
    })),
    codeMap: vi.fn(async () => ({ nodes: [], edges: [], node_count: 1, edge_count: 0, outbound: [] })),
    orgGalaxy: vi.fn(async () => ({ nodes: [], edges: [], collisions: [] })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
    config: vi.fn(async () => ({ hosted_mode: true, signup_mode: "closed" })),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    prds: vi.fn(async () => []),
    fleet: vi.fn(async () => ({
      agents: [], online: 0, total: 0, by_role: {}, posture: "single-agent", roles: [],
      presence_ttl_seconds: 90, heartbeat_interval_seconds: 30, review_queue: [],
      clusters: [], seats: [], waves: [],
    })),
    items: vi.fn(async () => []),
    shards: vi.fn(async () => []),
  },
}));

function fillOf(p: { accent: string; accent2?: string | null; pattern?: ProjectPattern | null }) {
  return projectFill({ accent: p.accent, accent2: p.accent2, pattern: p.pattern });
}

function painted(tile: HTMLElement): string {
  return tile.style.getPropertyValue("--tile-fill").trim();
}

function expectTile(tile: HTMLElement, p: Project) {
  expect(painted(tile)).toBe(fillOf(p));
  expect(tile.style.background).toBe("var(--tile-fill)");
  expect(tile).toHaveTextContent(p.tag);
  const shadow = (tile.getAttribute("style") ?? "").split("text-shadow:")[1] ?? "";
  expect(shadow.toLowerCase()).not.toContain(p.accent.replace("#", ""));
  if (p.accent2) expect(shadow.toLowerCase()).not.toContain(p.accent2.replace("#", ""));
}

function renderAt(path: string, ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>{ui}</ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectTile", () => {
  it("shows the tag on the fill, with a neutral glow and no accent in the shadow", () => {
    render(
      <ProjectTile
        project={{ tag: "GRPH", accent: "#ff00aa", accent2: "#00ffcc", pattern: "chevron" }}
        size={28}
      />,
    );
    const tile = screen.getByTestId("project-tile");
    expect(tile).toHaveTextContent("GRPH");
    expect(tile.style.width).toBe("28px");
    expect(tile.style.height).toBe("28px");
    // #0a0b0d. jsdom reports the used colour as rgb.
    expect(tile.style.color).toBe("rgb(10, 11, 13)");
    expect(tile.style.textShadow.replace(/\s/g, "")).toBe(TILE_GLOW.replace(/\s/g, ""));
    const shadow = (tile.getAttribute("style") ?? "").split("text-shadow:")[1] ?? "";
    expect(shadow.toLowerCase()).not.toContain("ff00aa");
    expect(shadow.toLowerCase()).not.toContain("00ffcc");
    expect(painted(tile)).toBe(
      projectFill({ accent: "#ff00aa", accent2: "#00ffcc", pattern: "chevron" }),
    );
  });

  it("draws a dash on the default accent when there is no project", () => {
    render(<ProjectTile project={null} />);
    const tile = screen.getByTestId("project-tile");
    expect(tile).toHaveTextContent("—");
    expect(painted(tile)).toBe("#c6f24e");
    expect(tile.className).toContain("h-[30px]");
  });
});

describe("the three tile surfaces share one fill", () => {
  beforeEach(() => {
    projects.list = [core, web, docs];
    vi.mocked(api.projects).mockResolvedValue(projects.list);
  });

  it("ProjectBar and the switcher paint each project with projectFill", async () => {
    const user = userEvent.setup();
    renderAt("/p/CORE/tracker", <ProjectBar />);
    const trigger = await screen.findByRole("button", { name: "Switch project" });
    expectTile(within(trigger).getByTestId("project-tile"), core);

    // The bar's edge stays the solid accent. It is not the tile fill.
    // The bar's edge stays the solid accent. It is not the tile fill.
    const stripe = document.querySelector("span.w-1");
    expect(stripe?.getAttribute("style")).toContain("rgb(198, 242, 78)");
    expect(stripe?.getAttribute("style")).not.toContain("linear-gradient");
    expect(stripe?.getAttribute("style")).not.toContain("var(");

    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Switch project" });
    for (const p of [core, web, docs]) {
      const option = within(dialog).getByRole("option", { name: new RegExp(p.name) });
      expectTile(within(option).getByTestId("project-tile"), p);
    }
    // Docs is stored solid: the tile is the accent, not an empty background.
    const docsTile = within(within(dialog).getByRole("option", { name: /Docs/ })).getByTestId("project-tile");
    expect(painted(docsTile)).toBe("#efd76a");
    expect(painted(docsTile)).not.toContain("linear-gradient");
  });

  it("Project Home paints the same fill as the bar", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/p/CORE"]}>
          <ProjectProvider>
            <Routes>
              <Route path="/p/:tag" element={<ProjectHome />} />
            </Routes>
          </ProjectProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const heading = await screen.findByRole("heading", { name: "Core", level: 1 });
    const tile = within(heading.parentElement!).getByTestId("project-tile");
    expectTile(tile, core);
    expect(tile.style.width).toBe("32px");
  });
});
