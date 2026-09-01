import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { LeftNav } from "@/components/shell/LeftNav";
import { HomeView } from "@/features/home/HomeView";
import { ProjectProvider } from "@/features/ProjectContext";
import { SettingsView } from "@/features/settings/SettingsView";

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

const org = { id: "org1", name: "Acme", plan: "pro", role: "owner" as const };

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({ items: 41, items_in_progress: 3, requests: 7, review: 5 })),
    dashboard: vi.fn(async () => ({
      items_total: 41, items_by_status: { backlog: 10, next: 8, in_progress: 3, review: 5, done: 15, blocked: 2 },
      effort_total: 0, done_count: 15, in_progress_count: 3, blocked_count: 2,
      requests_total: 7, requests_by_type: {}, requests_by_status: {},
      shard_count: 9, prd_count: 4, mcp_calls: 12, recent_items: [],
    })),
    fleet: vi.fn(async () => ({
      agents: [], online: 1, total: 1, by_role: {}, posture: "single-agent", roles: [],
      presence_ttl_seconds: 150, heartbeat_interval_seconds: 50, review_queue: [],
    })),
    orgs: vi.fn(async () => [org]),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    config: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
    mcpTools: vi.fn(async () => ({ live: 3, tools: [{ name: "claim_next", description: "x", params: [], calls: 1 }] })),
    platform: vi.fn(async () => null),
    members: vi.fn(async () => []),
    keys: vi.fn(async () => []),
  },
}));

function wrap(ui: ReactNode, path = "/tracker") {
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

describe("P28 self-host rail", () => {
  it("shows Plan children on /tracker and hides Dashboard, MCP Tools, Feedback Kit", async () => {
    wrap(<LeftNav />);
    expect(await screen.findByText("Tracker")).toBeInTheDocument();
    expect(screen.getByText("Plan")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText("Observe")).toBeInTheDocument();
    expect(screen.getByText("Home")).toBeInTheDocument();
    expect(screen.queryByText("Dashboard")).not.toBeInTheDocument();
    expect(screen.queryByText("MCP Tools")).not.toBeInTheDocument();
    expect(screen.queryByText("Feedback Kit")).not.toBeInTheDocument();
    expect(screen.queryByText("Galaxy")).not.toBeInTheDocument();
    expect(screen.queryByText("Users & access")).not.toBeInTheDocument();
    expect(await screen.findByText("41")).toBeInTheDocument();
  });

  it("expands Build when navigating from a Plan route via the section header", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(screen.getByRole("button", { name: "Build" }));
    expect(await screen.findByText("Code graph")).toBeInTheDocument();
    expect(screen.queryByText("Tracker")).not.toBeInTheDocument();
  });

  it("shows Memory and Lessons under Observe on /lessons (children are collapsed on /tracker)", async () => {
    wrap(<LeftNav />, "/lessons");
    expect(await screen.findByText("Lessons")).toBeInTheDocument();
    expect(screen.getByText("Memory")).toBeInTheDocument();
    expect(screen.getByText("Activity")).toBeInTheDocument();
  });

  it("keeps Lessons in OBSERVE next to Memory, and does not land Observe on /lessons", () => {
    const sources = import.meta.glob("../components/shell/LeftNav.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    const observe = src.match(/const OBSERVE = \[[\s\S]*?\];/)?.[0] ?? "";
    expect(observe).toContain('to: "/lessons"');
    expect(observe).toContain('to: "/memory-review"');

    const hostedStart = src.indexOf("const WORKSPACE");
    const hosted = src.slice(hostedStart, src.indexOf("as const", hostedStart));
    const mem = hosted.indexOf('to: "memory-review"');
    const les = hosted.indexOf('to: "lessons"');
    expect(mem).toBeGreaterThan(-1);
    expect(les).toBeGreaterThan(mem);
    expect(hosted.slice(mem, les).match(/to:/g)?.length).toBe(1);

    const def = src.slice(src.indexOf("observeDefault"), src.indexOf("observeDefault") + 180);
    expect(def).toContain("/memory-review");
    expect(def).toContain("/activity");
    expect(def).not.toContain("/lessons");
  });
});

describe("P28 hosted rail is untouched", () => {
  it("still shows Admin, Galaxy, Feedback Kit and the flat workspace list", async () => {
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    expect(await screen.findByText("Tracker")).toBeInTheDocument();
    expect(await screen.findByText("Users & access")).toBeInTheDocument();
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("MCP Tools")).toBeInTheDocument();
    expect(screen.getByText("Feedback Kit")).toBeInTheDocument();
    expect(screen.getByText("Galaxy")).toBeInTheDocument();
    expect(screen.getByText("Lessons")).toBeInTheDocument();
    expect(screen.getByText("Memory review")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Plan" })).not.toBeInTheDocument();
  });

  it("keeps Admin and Galaxy in the hosted source (sabotage: deleting those rows must fail)", async () => {
    const sources = import.meta.glob("../components/shell/LeftNav.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    expect(src).toContain('label="Galaxy"');
    expect(src).toContain('label="Users & access"');
    expect(src).toContain("function HostedLeftNav");
  });
});

describe("P28 Home", () => {
  it("renders KPIs from counts/dashboard, not as a quiet zero while loading", async () => {
    wrap(<HomeView />, "/home");
    expect(await screen.findByText("Home")).toBeInTheDocument();
    expect(await screen.findByText("41")).toBeInTheDocument();
    expect(screen.getByText("Triage")).toBeInTheDocument();
    expect(screen.getByText("Memory waiting for review")).toBeInTheDocument();
  });
});

describe("P28 Settings (self-host)", () => {
  it("groups This box / This project and has no Org heading", async () => {
    wrap(<SettingsView />, "/settings/deployment/providers");
    expect(await screen.findByText("This box")).toBeInTheDocument();
    expect(screen.getByText("This project")).toBeInTheDocument();
    expect(screen.getByText("Cloud / Sync")).toBeInTheDocument();
    expect(screen.getByText("MCP")).toBeInTheDocument();
    expect(screen.queryByText("Users & access")).not.toBeInTheDocument();
  });
});
