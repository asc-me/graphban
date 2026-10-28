import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "@/App";
import { LeftNav, SIDEBAR_STORAGE_KEY } from "@/components/shell/LeftNav";
import { AuthProvider } from "@/features/auth/AuthContext";
import { ProjectProvider } from "@/features/ProjectContext";
import { api } from "@/lib/api";

/**
 * GRPH-1141 — one sidebar for every route: 216px expanded or a 60px rail, `[` to toggle,
 * remembered across reloads, labels on the rail, no motion under reduced motion.
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
    me: vi.fn(async () => ({
      id: "u1", name: "Ada", handle: "ada", email: "ada@example.com", avatar: "#c6f24e", initials: "A",
    })),
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({
      items: 41, items_in_progress: 3, requests: 7, review: 5, triage_new: 4, lessons_published: 6,
    })),
    items: vi.fn(async () => []),
    prds: vi.fn(async () => []),
    shards: vi.fn(async () => []),
    orgs: vi.fn(async () => [{ id: "org1", name: "Acme", plan: "pro", role: "owner" }]),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    config: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
    mcpTools: vi.fn(async () => ({ live: 0, tools: [] })),
    platform: vi.fn(async () => null),
  },
}));

// The route sweep is about the shell each route mounts, not the page inside it. Stub
// every page so a route renders without that page's own data.
const { stub } = vi.hoisted(() => ({
  stub: (name: string) => async () => {
    const { createElement } = await import("react");
    return { [name]: () => createElement("div", { "data-testid": "page" }, name) };
  },
}));
vi.mock("@/features/tracker/TrackerView", stub("TrackerView"));
vi.mock("@/features/requests/RequestsView", stub("RequestsView"));
vi.mock("@/features/triage/TriageView", stub("TriageView"));
vi.mock("@/features/links/LinksGraphView", stub("LinksGraphView"));
vi.mock("@/features/code/CodeGraphView", stub("CodeGraphView"));
vi.mock("@/features/stack/StackView", stub("StackView"));
vi.mock("@/features/roadmap/RoadmapView", stub("RoadmapView"));
vi.mock("@/features/mcp/McpToolsView", stub("McpToolsView"));
vi.mock("@/features/fleet/FleetView", stub("FleetView"));
vi.mock("@/features/fleet/FleetV2View", stub("FleetV2View"));
vi.mock("@/features/fleet/OutpostsView", stub("OutpostsView"));
vi.mock("@/features/harness/HarnessView", stub("HarnessView"));
vi.mock("@/features/activity/ActivityView", stub("ActivityView"));
vi.mock("@/features/live/LiveView", stub("LiveView"));
vi.mock("@/features/memory/MemoryReviewView", stub("MemoryReviewView"));
vi.mock("@/features/memory/MemoryTriageView", stub("MemoryTriageView"));
vi.mock("@/features/lessons/LessonsView", stub("LessonsView"));
vi.mock("@/features/prds/PrdListView", stub("PrdListView"));
vi.mock("@/features/prds/PrdEditorView", stub("PrdEditorView"));
vi.mock("@/features/feedback/FeedbackKitView", stub("FeedbackKitView"));
vi.mock("@/features/home/HomeView", stub("HomeView"));
vi.mock("@/features/projecthome/ProjectHome", stub("ProjectHome"));
vi.mock("@/features/settings/SettingsView", stub("SettingsView"));
vi.mock("@/features/usage/UsageView", stub("UsageView"));
vi.mock("@/features/profile/ProfileView", stub("ProfileView"));
vi.mock("@/components/shell/AgentSidebar", () => ({ AgentSidebar: () => null }));
vi.mock("@/features/docs/DocsReader", () => ({ DocsReader: () => null }));

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

const sidebar = () => document.querySelector<HTMLElement>('aside[aria-label="Sidebar"]')!;
const layer = (name: "rail" | "expanded") =>
  sidebar().querySelector<HTMLElement>(`[data-layer="${name}"]`)!;

function stubReducedMotion(reduce: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: reduce && q.includes("prefers-reduced-motion"),
    media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }));
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("GRPH-1141 the [ hotkey and its memory", () => {
  it("toggles expanded and rail, and the rail survives a reload", async () => {
    const view = wrap(<LeftNav />);
    await screen.findByText("Tracker");
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
    expect(sidebar().style.width).toBe("216px");

    fireEvent.keyDown(window, { key: "[" });
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    expect(sidebar().style.width).toBe("60px");
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("1");

    view.unmount();
    wrap(<LeftNav />);
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    // Where you are still shows: the current section's pages are on the rail.
    expect(await screen.findByRole("link", { name: "Tracker" })).toHaveAttribute("aria-current", "page");

    fireEvent.keyDown(window, { key: "[" });
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("0");
  });

  it("does nothing while focus is in an input, textarea, select or contenteditable", async () => {
    wrap(
      <>
        <LeftNav />
        <input aria-label="field" />
        <textarea aria-label="area" />
        <select aria-label="pick"><option>a</option></select>
        <div aria-label="rich" contentEditable suppressContentEditableWarning />
      </>,
    );
    await screen.findByText("Tracker");
    for (const name of ["field", "area", "pick", "rich"]) {
      const el = screen.getByLabelText(name);
      // jsdom has no isContentEditable; the browser does.
      if (name === "rich") Object.defineProperty(el, "isContentEditable", { value: true });
      fireEvent.keyDown(el, { key: "[" });
      expect(sidebar(), name).toHaveAttribute("data-collapsed", "false");
    }
    // Sabotage guard: the same key from the body does toggle, so the loop above can fail.
    fireEvent.keyDown(document.body, { key: "[" });
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
  });

  it("the footer buttons toggle the same state", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />);
    await user.click(await screen.findByRole("button", { name: /Collapse sidebar/ }));
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    await user.click(screen.getByRole("button", { name: "Expand sidebar" }));
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
  });
});

describe("GRPH-1141 the current page", () => {
  it("has aria-current and the lime bar when expanded", async () => {
    wrap(<LeftNav />, "/requests");
    const link = within(layer("expanded")).getByRole("link", { name: /Requests/ });
    await waitFor(() => expect(link).toHaveAttribute("aria-current", "page"));
    expect(link.querySelector("[data-current-bar]")).not.toBeNull();
    const other = within(layer("expanded")).getByRole("link", { name: /Tracker/ });
    expect(other).not.toHaveAttribute("aria-current");
    expect(other.querySelector("[data-current-bar]")).toBeNull();
  });

  it("has aria-current and the lime bar on the rail", async () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");
    wrap(<LeftNav />, "/requests");
    const link = within(layer("rail")).getByRole("link", { name: "Requests" });
    expect(link).toHaveAttribute("aria-current", "page");
    expect(link.querySelector("[data-current-bar]")).not.toBeNull();
    expect(link.className).toContain("text-accent");
    const other = within(layer("rail")).getByRole("link", { name: "Tracker" });
    expect(other.querySelector("[data-current-bar]")).toBeNull();
    // Other sections are one icon each, opening a menu rather than linking away.
    expect(within(layer("rail")).getByRole("button", { name: "Build" })).toHaveAttribute("aria-haspopup", "menu");
    expect(within(layer("rail")).queryByRole("link", { name: "Code graph" })).toBeNull();
  });

  it("hides the inactive layer from assistive tech and focus", async () => {
    wrap(<LeftNav />);
    await screen.findByText("Tracker");
    expect(layer("rail")).toHaveAttribute("aria-hidden", "true");
    expect(layer("rail")).toHaveAttribute("inert");
    fireEvent.keyDown(window, { key: "[" });
    expect(layer("expanded")).toHaveAttribute("aria-hidden", "true");
    expect(layer("expanded")).toHaveAttribute("inert");
    expect(layer("rail")).not.toHaveAttribute("aria-hidden", "true");
  });
});

describe("GRPH-1141 rail labels", () => {
  it("shows section, label and count on hover and focus", async () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");
    wrap(<LeftNav />, "/tracker");
    const tip = document.querySelector<HTMLElement>('[role="tooltip"]')!;
    const requests = within(layer("rail")).getByRole("link", { name: "Requests" });
    await screen.findAllByText("41", { exact: false }).catch(() => undefined);
    await waitFor(() => {
      fireEvent.mouseEnter(requests);
      expect(tip).toHaveTextContent("PLAN");
      expect(tip).toHaveTextContent("Requests");
      expect(tip).toHaveTextContent("7");
    });
    expect(tip.style.opacity).toBe("1");
    fireEvent.mouseLeave(requests);
    expect(tip.style.opacity).toBe("0");

    act(() => within(layer("rail")).getByRole("button", { name: "Expand sidebar" }).focus());
    expect(tip).toHaveTextContent("Expand sidebar");
    expect(tip).toHaveTextContent("[");
  });

  it("is suppressed while a menu is open", async () => {
    const user = userEvent.setup();
    localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");
    wrap(<LeftNav />, "/tracker");
    const tip = document.querySelector<HTMLElement>('[role="tooltip"]')!;
    const build = within(layer("rail")).getByRole("button", { name: "Build" });
    await user.click(build);
    await screen.findByRole("menu");
    fireEvent.mouseEnter(build);
    expect(tip.style.opacity).toBe("0");
  });

  it("never shows in the expanded sidebar", async () => {
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    const tip = document.querySelector<HTMLElement>('[role="tooltip"]')!;
    fireEvent.mouseEnter(within(layer("rail")).getByRole("link", { name: "Requests", hidden: true }));
    expect(tip.style.opacity).toBe("0");
  });
});

describe("GRPH-1141 motion", () => {
  it("animates width, fade and slide on the 240/180ms curve", async () => {
    stubReducedMotion(false);
    wrap(<LeftNav />);
    await screen.findByText("Tracker");
    expect(sidebar().style.transition).toBe("width 240ms cubic-bezier(.2,.8,.2,1)");
    expect(layer("rail").style.transition).toContain("opacity 180ms");
    expect(layer("expanded").style.transition).toContain("transform 240ms");
  });

  it("applies no width or transform transition under prefers-reduced-motion", async () => {
    stubReducedMotion(true);
    wrap(<LeftNav />);
    await screen.findByText("Tracker");
    fireEvent.keyDown(window, { key: "[" });
    for (const el of [sidebar(), layer("rail"), layer("expanded")]) {
      expect(el.style.transition).not.toMatch(/width|transform/);
    }
    expect(sidebar().style.transition).toBe("none");
  });
});

/**
 * Every route mounts the one sidebar and none mounts the old accordion. The accordion's
 * signature was every section as an in-place disclosure (`hosted-nav-*` panels for the
 * closed ones too); now only the current section is inline and the rest open menus.
 */
const VIEWS = [
  "tracker", "requests", "triage", "links", "code", "stack", "roadmap", "fleet.v1", "fleet.v2",
  "outposts", "harness", "activity", "live", "memory-review", "memory-triage", "lessons",
  "lessons/l1", "prds", "prds/p1", "feedback-kit",
];
const SHARED = ["/settings", "/settings/project/mcp", "/usage", "/profile"];
const SELF_HOST = [...VIEWS.map((v) => `/${v}`), "/home", ...SHARED];
const HOSTED = [...VIEWS.map((v) => `/p/CORE/${v}`), "/p/CORE/mcp-tools", "/p/CORE", ...SHARED];

async function renderAppAt(path: string, hosted: boolean) {
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: hosted, signup_mode: "closed" });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByTestId("page");
  return view;
}

function expectNewSidebar(path: string) {
  const asides = document.querySelectorAll('aside[aria-label="Sidebar"]');
  expect(asides, path).toHaveLength(1);
  expect(document.querySelectorAll("aside"), `${path}: a second nav is mounted`).toHaveLength(1);
  expect(document.querySelector('[id^="hosted-nav-"]'), path).toBeNull();
  const nav = within(layer("expanded"));
  const inline = document.querySelectorAll('[id^="nav-"]');
  expect(inline.length, `${path}: at most one section is inline`).toBeLessThanOrEqual(1);
  const menus = ["Plan", "Build", "Observe"].filter(
    (s) => nav.getByRole("button", { name: s }).getAttribute("aria-haspopup") === "menu",
  );
  expect(menus.length, `${path}: the other sections open menus`).toBe(3 - inline.length);
  expect(layer("rail"), path).not.toBeNull();
  expect(nav.getByRole("button", { name: /Collapse sidebar/ }), path).toBeInTheDocument();
}

describe("GRPH-1141 route sweep", () => {
  afterEach(() => {
    vi.mocked(api.config).mockResolvedValue({ hosted_mode: false, signup_mode: "closed" });
  });

  it.each(SELF_HOST)("self-host %s mounts the new sidebar", async (path) => {
    await renderAppAt(path, false);
    expectNewSidebar(path);
  });

  it.each(HOSTED)("hosted %s mounts the new sidebar", async (path) => {
    await renderAppAt(path, true);
    expectNewSidebar(path);
  });

  it("the old per-mode navs are gone from the source", () => {
    const sources = import.meta.glob("../components/shell/LeftNav.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    expect(src).not.toContain("function HostedLeftNav");
    expect(src).not.toContain("function SelfHostLeftNav");
  });
});
