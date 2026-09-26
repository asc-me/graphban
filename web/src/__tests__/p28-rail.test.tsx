import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
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

/** Renders the live pathname so a test can tell "expanded" from "navigated". */
function Here() {
  return <div data-testid="here">{useLocation().pathname}</div>;
}

function wrap(ui: ReactNode, path = "/tracker") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="*" element={ui} />
          </Routes>
          <Here />
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

  it("expands Build on click without leaving the current page", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(screen.getByRole("button", { name: "Build" }));
    expect(await screen.findByText("Code graph")).toBeInTheDocument();
    expect(screen.getByText("Fleet.v2")).toBeInTheDocument();
    expect(screen.getByText("Fleet.v1")).toBeInTheDocument();
    expect(screen.getByText("Outposts")).toBeInTheDocument();
    expect(screen.queryByText("Tracker")).not.toBeInTheDocument();
    // A header is a disclosure, not a link: opening Build must not move you off /tracker.
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });

  it("collapses a section when its own header is clicked again", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(screen.getByRole("button", { name: "Plan" }));
    expect(screen.queryByText("Tracker")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan" })).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByRole("button", { name: "Plan" }));
    expect(screen.getByText("Tracker")).toBeInTheDocument();
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });

  it("reopens the owning section when the route moves into another one", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(screen.getByRole("button", { name: "Observe" }));
    expect(screen.getByText("Activity")).toBeInTheDocument();
    // Following a child link changes the route, and the rail follows the route.
    await user.click(screen.getByText("Activity"));
    expect(screen.getByTestId("here")).toHaveTextContent("/activity");
    expect(screen.getByRole("button", { name: "Observe" })).toHaveAttribute("aria-expanded", "true");
  });

  it("shows Memory and Lessons under Observe on /lessons (children are collapsed on /tracker)", async () => {
    wrap(<LeftNav />, "/lessons");
    expect(await screen.findByText("Lessons")).toBeInTheDocument();
    expect(screen.getByText("Memory")).toBeInTheDocument();
    expect(screen.getByText("Activity")).toBeInTheDocument();
  });

  it("shows Live next to Activity under Observe on /live", async () => {
    wrap(<LeftNav />, "/live");
    expect(await screen.findByText("Live")).toBeInTheDocument();
    expect(screen.getByText("Activity")).toBeInTheDocument();
    expect(screen.getByText("Memory")).toBeInTheDocument();
    expect(screen.getByText("Lessons")).toBeInTheDocument();
  });

  it("keeps Lessons in OBSERVE next to Memory, and keeps headers non-navigating", () => {
    const sources = import.meta.glob("../components/shell/LeftNav.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    const build = src.match(/const BUILD = \[[\s\S]*?\];/)?.[0] ?? "";
    expect(build).toContain('to: "/fleet.v2"');
    expect(build).toContain('to: "/fleet.v1"');
    expect(build).not.toContain('to: "/fleet",');

    const observe = src.match(/const OBSERVE = \[[\s\S]*?\];/)?.[0] ?? "";
    expect(observe).toContain('to: "/lessons"');
    expect(observe).toContain('to: "/memory-review"');
    expect(observe).toContain('to: "/live"');
    expect(observe).toContain('to: "/activity"');

    const hostedObserve = src.match(/const HOSTED_OBSERVE = \[[\s\S]*?\];/)?.[0] ?? "";
    const mem = hostedObserve.indexOf('to: "memory-review"');
    const les = hostedObserve.indexOf('to: "lessons"');
    expect(mem).toBeGreaterThan(-1);
    expect(les).toBeGreaterThan(mem);
    expect(hostedObserve.slice(mem, les).match(/to:/g)?.length).toBe(1);

    const act = hostedObserve.indexOf('to: "activity"');
    const live = hostedObserve.indexOf('to: "live"');
    expect(act).toBeGreaterThan(-1);
    expect(live).toBeGreaterThan(act);
    expect(hostedObserve.slice(act, live).match(/to:/g)?.length).toBe(1);

    // A section header expands; it never navigates. No default landing page may creep back in.
    const header = src.match(/function SectionHeader\(\{[\s\S]*?\n\}/)?.[0] ?? "";
    expect(header).toContain("onToggle");
    expect(header).not.toContain("navigate(");
    expect(src).not.toContain("observeDefault");
  });
});

describe("GRPH-940 hosted rail uses Plan / Build / Observe", () => {
  it("opens Plan on /p/CORE/tracker and shows Admin, Galaxy, Feedback Kit", async () => {
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    expect(await screen.findByText("Tracker")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Requests")).toBeInTheDocument();
    expect(screen.getByText("Triage")).toBeInTheDocument();
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("PRDs")).toBeInTheDocument();
    expect(screen.getByText("Roadmap")).toBeInTheDocument();
    expect(await screen.findByText("Users & access")).toBeInTheDocument();
    expect(screen.getByText("Feedback Kit")).toBeInTheDocument();
    expect(screen.getByText("Galaxy")).toBeInTheDocument();
    // Build and Observe are collapsed — their children are not in the document.
    expect(screen.queryByText("Code graph")).not.toBeInTheDocument();
    expect(screen.queryByText("Memory review")).not.toBeInTheDocument();
  });

  it("expands Build on click without changing the route", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    await screen.findByText("Tracker");
    await user.click(screen.getByRole("button", { name: "Build" }));
    expect(await screen.findByText("Code graph")).toBeInTheDocument();
    expect(screen.getByText("MCP Tools")).toBeInTheDocument();
    expect(screen.getByText("Fleet.v2")).toBeInTheDocument();
    // Plan collapsed, Build expanded.
    expect(screen.queryByText("Tracker")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build" })).toHaveAttribute("aria-expanded", "true");
    // Header click must not navigate — same trap as self-host (GRPH-P28 rev2).
    expect(screen.getByTestId("here")).toHaveTextContent("/p/CORE/tracker");
  });

  it("opens the owning section when the route is in another group", async () => {
    wrap(<LeftNav hosted />, "/p/CORE/activity");
    expect(await screen.findByText("Activity")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText("Memory review")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Observe" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByText("Tracker")).not.toBeInTheDocument();
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

  it("sabotage: Plan, Build and Observe must exist in the hosted tree", async () => {
    const sources = import.meta.glob("../components/shell/LeftNav.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    // The hosted render must use SectionHeader with all three labels — removing a group
    // must fail these assertions, not silently produce a flat list.
    const hostedFn = src.slice(src.indexOf("function HostedLeftNav"), src.indexOf("function SelfHostLeftNav"));
    expect(hostedFn).toContain('label="Plan"');
    expect(hostedFn).toContain('label="Build"');
    expect(hostedFn).toContain('label="Observe"');
    expect(hostedFn).toContain("HOSTED_PLAN");
    expect(hostedFn).toContain("HOSTED_BUILD");
    expect(hostedFn).toContain("HOSTED_OBSERVE");
  });
});

describe("P28 Home", () => {
  it("renders KPIs from counts/dashboard, not as a quiet zero while loading", async () => {
    wrap(<HomeView />, "/home");
    expect(await screen.findByText("Home")).toBeInTheDocument();
    // Wait for counts/dashboard before asserting hierarchy (GRPH-925).
    expect(await screen.findByText("41")).toBeInTheDocument();
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    // 3 in progress + 5 in review + 2 blocked + 5 memory queue = 15
    expect(screen.getByText("15")).toBeInTheDocument();
    expect(screen.getByText("Inventory")).toBeInTheDocument();
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
    expect(screen.getByText("MCP Tools")).toBeInTheDocument();
    expect(screen.queryByText(/^MCP$/)).not.toBeInTheDocument();
    expect(screen.queryByText("Users & access")).not.toBeInTheDocument();
  });
});
