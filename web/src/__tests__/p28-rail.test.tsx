import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "@/App";
import { LeftNav } from "@/components/shell/LeftNav";
import { AuthProvider } from "@/features/auth/AuthContext";
import { HomeView } from "@/features/home/HomeView";
import { ProjectProvider } from "@/features/ProjectContext";
import { SettingsView } from "@/features/settings/SettingsView";
import { api } from "@/lib/api";

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

const org = { id: "org1", name: "Acme", plan: "pro", role: "owner" as const };

vi.mock("@/lib/api", () => ({
  // The whole-app render at the bottom of this file signs in through the real AuthProvider.
  hasSession: () => true,
  api: {
    me: vi.fn(async () => ({
      id: "u1", name: "Ada", handle: "ada", email: "ada@example.com", avatar: "#c6f24e", initials: "A",
    })),
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({
      items: 41, items_in_progress: 3, requests: 7, review: 5, triage_new: 4, lessons_published: 6,
    })),
    // Collections the shell chrome asks for. Empty on purpose: the redirect tests assert on
    // where the URL landed, and a populated palette/sidebar only adds noise around it.
    items: vi.fn(async () => []),
    prds: vi.fn(async () => []),
    shards: vi.fn(async () => []),
    dashboard: vi.fn(async () => ({
      items_total: 41, items_by_status: { backlog: 10, next: 8, in_progress: 3, review: 5, done: 15, blocked: 2 },
      effort_total: 0, done_count: 15, in_progress_count: 3, blocked_count: 2,
      requests_total: 7, requests_by_type: {}, requests_by_status: {},
      shard_count: 9, prd_count: 4, mcp_calls: 12, recent_items: [],
    })),
    codeMap: vi.fn(async () => ({ nodes: [], edges: [], node_count: 12, edge_count: 0, outbound: [] })),
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
  it("shows Plan children on /tracker, a top-level Dashboard, and no MCP Tools", async () => {
    wrap(<LeftNav />);
    expect(await screen.findByText("Tracker")).toBeInTheDocument();
    expect(screen.getByText("Plan")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText("Observe")).toBeInTheDocument();
    const plan = document.getElementById("nav-plan");
    expect(plan).not.toBeNull();
    expect(within(plan!).queryByText("Dashboard")).not.toBeInTheDocument();
    expect(within(plan!).getAllByRole("link")).toHaveLength(5);
    const dash = screen.getByRole("link", { name: "Dashboard" });
    expect(dash).toHaveAttribute("href", "/home");
    expect(dash).not.toHaveAttribute("aria-current");
    expect(screen.queryByText("Home")).not.toBeInTheDocument();
    expect(screen.queryByText("MCP Tools")).not.toBeInTheDocument();
    expect(screen.getByText("Feedback Kit")).toBeInTheDocument();
    expect(screen.queryByText("Galaxy")).not.toBeInTheDocument();
    expect(screen.queryByText("Users & access")).not.toBeInTheDocument();
    expect(await screen.findByText("41")).toBeInTheDocument();
  });

  it("opens Build as a menu beside the sidebar without leaving the current page", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(screen.getByRole("button", { name: "Build" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText("Code graph")).toBeInTheDocument();
    expect(within(menu).getByText("Fleet")).toBeInTheDocument();
    expect(within(menu).queryByText("Fleet.v1")).not.toBeInTheDocument();
    expect(within(menu).getByText("Outposts")).toBeInTheDocument();
    // Plan stays inline: it is the section that owns the page you are on.
    expect(document.getElementById("nav-plan")).not.toBeNull();
    // Opening a section never navigates: Build must not move you off /tracker.
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
    await user.click(await screen.findByRole("menuitem", { name: /Activity/ }));
    // Choosing a page changes the route, and the sidebar follows the route.
    expect(screen.getByTestId("here")).toHaveTextContent("/activity");
    expect(screen.getByRole("button", { name: "Observe" })).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById("nav-observe")).not.toBeNull();
    expect(document.getElementById("nav-plan")).toBeNull();
  });

  it("shows Memory and Lessons under Observe on /lessons (children are collapsed on /tracker)", async () => {
    wrap(<LeftNav />, "/lessons");
    expect(await screen.findByText("Lessons")).toBeInTheDocument();
    expect(screen.getByText("Memory review")).toBeInTheDocument();
    expect(screen.getByText("Activity")).toBeInTheDocument();
  });

  it("shows Live next to Activity under Observe on /live", async () => {
    wrap(<LeftNav />, "/live");
    expect(await screen.findByText("Live")).toBeInTheDocument();
    expect(screen.getByText("Activity")).toBeInTheDocument();
    expect(screen.getByText("Memory review")).toBeInTheDocument();
    expect(screen.getByText("Lessons")).toBeInTheDocument();
  });

  it("keeps Lessons in OBSERVE next to Memory, and keeps headers non-navigating", () => {
    const sources = import.meta.glob("../components/shell/LeftNav.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    const build = src.match(/key: "build"[\s\S]*?\],\n  \}/)?.[0] ?? "";
    expect(build).toContain('view: "fleet",');
    expect(build).not.toContain('view: "fleet.v');

    const observe = src.match(/key: "observe"[\s\S]*?\],\n  \}/)?.[0] ?? "";
    const mem = observe.indexOf('view: "memory-review"');
    const les = observe.indexOf('view: "lessons"');
    expect(mem).toBeGreaterThan(-1);
    expect(les).toBeGreaterThan(mem);
    expect(observe.slice(mem, les).match(/view:/g)?.length).toBe(2);

    const act = observe.indexOf('view: "activity"');
    const live = observe.indexOf('view: "live"');
    expect(act).toBeGreaterThan(-1);
    expect(live).toBeGreaterThan(act);
    expect(observe.slice(act, live).match(/view:/g)?.length).toBe(1);

    const harness = observe.indexOf('view: "harness"');
    expect(harness).toBeGreaterThan(live);
    expect(observe.slice(live, harness).match(/view:/g)?.length).toBe(1);

    // A section header expands; it never navigates. No default landing page may creep back in.
    const header = src.match(/function SectionHeader\(\{[\s\S]*?\n\}/)?.[0] ?? "";
    expect(header).toContain("onToggle");
    expect(header).not.toContain("navigate(");
    expect(src).not.toContain("observeDefault");
  });
});

describe("GRPH-940 hosted rail uses Plan / Build / Observe", () => {
  it("opens Plan on /p/CORE/tracker and shows Admin and Feedback Kit", async () => {
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    expect(await screen.findByText("Tracker")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan" })).toHaveAttribute("aria-expanded", "true");
    const plan = document.getElementById("nav-plan");
    expect(plan).not.toBeNull();
    expect(within(plan!).getByText("Requests")).toBeInTheDocument();
    expect(within(plan!).getByText("Triage")).toBeInTheDocument();
    expect(within(plan!).queryByText("Dashboard")).not.toBeInTheDocument();
    expect(within(plan!).getByText("PRDs")).toBeInTheDocument();
    expect(within(plan!).getByText("Roadmap")).toBeInTheDocument();
    // Five Plan links. Putting Dashboard back under Plan fails this count.
    expect(within(plan!).getAllByRole("link")).toHaveLength(5);
    const dash = screen.getByRole("link", { name: "Dashboard" });
    expect(dash).toHaveAttribute("href", "/p/CORE");
    expect(dash).not.toHaveAttribute("aria-current");
    expect(await screen.findByRole("link", { name: "Admin" })).toBeInTheDocument();
    expect(screen.getByText("Feedback Kit")).toBeInTheDocument();
    // Build and Observe are closed menus — their pages are not in the document.
    expect(screen.queryByText("Code graph")).not.toBeInTheDocument();
    expect(screen.queryByText("Memory review")).not.toBeInTheDocument();
  });

  it("expands Build on click without changing the route", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    await screen.findByText("Tracker");
    await user.click(screen.getByRole("button", { name: "Build" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText("Code graph")).toBeInTheDocument();
    expect(within(menu).getByText("MCP Tools")).toBeInTheDocument();
    expect(within(menu).getByText("Fleet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build" })).toHaveAttribute("aria-expanded", "true");
    // Opening must not navigate — same trap as self-host (GRPH-P28 rev2).
    expect(screen.getByTestId("here")).toHaveTextContent("/p/CORE/tracker");
    await user.click(within(menu).getByRole("menuitem", { name: /Code graph/ }));
    expect(screen.getByTestId("here")).toHaveTextContent("/p/CORE/code");
    expect(document.getElementById("nav-build")).not.toBeNull();
  });

  it("opens the owning section when the route is in another group", async () => {
    wrap(<LeftNav hosted />, "/p/CORE/activity");
    expect(await screen.findByText("Activity")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText("Harness")).toBeInTheDocument();
    expect(screen.getByText("Memory review")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Observe" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByText("Tracker")).not.toBeInTheDocument();
  });

  it("shows hosted Harness under Observe and Usage link in the footer", async () => {
    wrap(<LeftNav hosted />, "/p/CORE/live");
    expect(await screen.findByText("Harness")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Usage" })).toHaveAttribute("href", "/usage");
  });

  it("sabotage: the Admin row and all three sections stay in the source", () => {
    const sources = import.meta.glob("../components/shell/LeftNav.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    expect(src).toContain('label="Admin"');
    expect(src).toContain('key: "plan", label: "Plan"');
    expect(src).toContain('key: "build", label: "Build"');
    expect(src).toContain('key: "observe", label: "Observe"');
    // Dashboard sits above the sections, not inside Plan.
    const plan = src.match(/key: "plan"[\s\S]*?\],\n  \}/)?.[0] ?? "";
    expect(plan).not.toContain("dashboard");
    expect(plan.match(/label:/g)?.length).toBe(6);
  });
});

describe("P28 Home", () => {
  // Self-host home is the shared project home (GRPH-1067). "Needs attention" was
  // removed on purpose; that reversal of GRPH-925 is the layout.
  it("renders the shared home from counts and dashboard", async () => {
    wrap(<HomeView />, "/home");
    expect(await screen.findByRole("heading", { name: "Core" })).toBeInTheDocument();
    expect(await screen.findByText("41")).toBeInTheDocument();
    expect(screen.getByText("In flight")).toBeInTheDocument();
    expect(screen.queryByText("Needs attention")).not.toBeInTheDocument();
    expect(screen.queryByText("Inventory")).not.toBeInTheDocument();
    expect(screen.queryByText("Memory waiting for review")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Cross-project dependencies" })).not.toBeInTheDocument();

    const jump = screen.getByRole("region", { name: "Jump to" });
    expect(jump).toHaveTextContent("3 in flight");
    expect(jump).toHaveTextContent("4 new");
    expect(jump).toHaveTextContent("4 docs");
    expect(jump).toHaveTextContent("12 nodes");
    expect(jump).toHaveTextContent("5 to review");
    expect(jump).toHaveTextContent("6 published");
    expect(jump).toHaveTextContent("1 online");
    expect(jump).toHaveTextContent("1 on now");
  });

  it("wires jump tiles to flat self-host paths", async () => {
    wrap(<HomeView />, "/home");
    const jump = await screen.findByRole("region", { name: "Jump to" });
    expect(within(jump).getByRole("link", { name: /Tracker/ })).toHaveAttribute("href", "/tracker");
    expect(within(jump).getByRole("link", { name: /Triage/ })).toHaveAttribute("href", "/triage");
    expect(within(jump).getByRole("link", { name: /^Memory/ })).toHaveAttribute("href", "/memory-review");
    expect(within(jump).getByRole("link", { name: /Fleet/ })).toHaveAttribute("href", "/fleet");
    expect(within(jump).getByRole("link", { name: /Lessons/ })).toHaveAttribute("href", "/lessons");
    expect(within(jump).getByRole("link", { name: /^Live/ })).toHaveAttribute("href", "/live");
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
    expect(screen.queryByText("Feedback Kit")).not.toBeInTheDocument();
  });
});

/**
 * The whole app at one URL — real router, real shell, real views.
 *
 * GRPH-978: `/settings/project/feedback-kit` is a `<Navigate>` registered in App.tsx for
 * both modes, and deleting it left the entire suite green, because every other test here
 * mounts one component and never asks the router where a URL lands. The redirect IS the
 * wiring, so the wiring is what has to be rendered.
 */
function renderAppAt(path: string, hosted: boolean) {
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: hosted, signup_mode: "closed" });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <App />
          <Here />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("GRPH-1066 /dashboard is project home and the nav entry is current there", () => {
  afterEach(() => {
    vi.mocked(api.config).mockResolvedValue({ hosted_mode: false, signup_mode: "closed" });
  });

  it("hosted /p/:tag/dashboard lands on project home with Dashboard current", async () => {
    renderAppAt("/p/CORE/dashboard", true);
    expect(await screen.findByRole("heading", { name: "Core" })).toBeInTheDocument();
    expect(screen.getByTestId("here")).toHaveTextContent(/^\/p\/CORE$/);
    const dash = screen.getByRole("link", { name: "Dashboard" });
    expect(dash).toHaveAttribute("href", "/p/CORE");
    expect(dash).toHaveAttribute("aria-current", "page");
    // This page owns no section, so Plan stays closed. Jump tiles still link to Tracker.
    expect(screen.getByRole("button", { name: "Plan" })).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById("nav-plan")).toBeNull();
  });

  it("hosted flat /dashboard resolves to the same home", async () => {
    renderAppAt("/dashboard", true);
    expect(await screen.findByRole("heading", { name: "Core" })).toBeInTheDocument();
    expect(screen.getByTestId("here")).toHaveTextContent(/^\/p\/CORE$/);
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
  });

  it("self-host /dashboard still lands on /home and highlights Dashboard", async () => {
    renderAppAt("/dashboard", false);
    expect(await screen.findByRole("heading", { name: "Core" })).toBeInTheDocument();
    expect(screen.getByTestId("here")).toHaveTextContent(/^\/home$/);
    const dash = screen.getByRole("link", { name: "Dashboard" });
    expect(dash).toHaveAttribute("href", "/home");
    expect(dash).toHaveAttribute("aria-current", "page");
  });
});

describe("GRPH-978 the feedback-kit settings bookmark redirects", () => {
  // `config` is module-scoped, so the hosted test would otherwise leak into every later one.
  afterEach(() => {
    vi.mocked(api.config).mockResolvedValue({ hosted_mode: false, signup_mode: "closed" });
  });

  it("self-host: lands on /feedback-kit with the Kit, not the settings pane", async () => {
    renderAppAt("/settings/project/feedback-kit", false);

    expect(await screen.findByRole("heading", { name: "Feedback Kit" })).toBeInTheDocument();
    // Anchored: "/settings/project/feedback-kit" also CONTAINS "/feedback-kit", so a
    // substring match would pass on the unredirected URL this test exists to catch.
    expect(screen.getByTestId("here")).toHaveTextContent(/^\/feedback-kit$/);
    // The quiet failure. Without the redirect this URL matches `/settings/*` and renders
    // SettingsView — a real page, plausible in a screenshot, entirely the wrong one.
    expect(screen.queryByText("This box")).not.toBeInTheDocument();
  });

  it("hosted: resolves through FlatRedirect to the project's tag URL", async () => {
    renderAppAt("/settings/project/feedback-kit", true);

    expect(await screen.findByRole("heading", { name: "Feedback Kit" })).toBeInTheDocument();
    // Two hops: the Navigate to /feedback-kit, then the flat hosted resolver into /p/:tag.
    expect(screen.getByTestId("here")).toHaveTextContent(/^\/p\/CORE\/feedback-kit$/);
    expect(screen.queryByText("This box")).not.toBeInTheDocument();
  });
});
