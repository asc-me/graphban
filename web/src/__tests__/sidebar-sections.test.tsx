import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LeftNav, SECTIONS, SIDEBAR_STORAGE_KEY } from "@/components/shell/LeftNav";
import { ProjectProvider } from "@/features/ProjectContext";

/**
 * GRPH-1222 — expanded, the sections are an accordion: each header folds its pages out inline,
 * one section at a time, and nothing opens a flyout. GRPH-1142 — on the collapsed rail the
 * current section's pages are icons and every other section is one icon that opens a 264px
 * flyout beside the sidebar. Opening never navigates.
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

function Here() {
  return <div data-testid="here">{useLocation().pathname}</div>;
}

function wrap(ui: ReactNode, path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="*" element={ui} />
          </Routes>
          <Here />
          <button type="button">outside</button>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const expanded = () => within(document.querySelector<HTMLElement>('[data-layer="expanded"]')!);
const rail = () => within(document.querySelector<HTMLElement>('[data-layer="rail"]')!);
/** Flyouts a user can see: a closed one stays mounted to fade out, but hidden. */
const openFlyouts = () => screen.queryAllByRole("dialog");

function stubReducedMotion(reduce: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: reduce && q.includes("reduce"), media: q, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
  }));
}

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("GRPH-1142 grouping", () => {
  it("lists the decided pages in the decided order", () => {
    expect(SECTIONS.map((s) => [s.label, s.pages.map((p) => p.label)])).toEqual([
      ["Plan", ["Triage", "Tracker", "Requests", "PRDs", "Roadmap"]],
      ["Build", ["Links", "Code graph", "Stack", "Rules", "MCP Tools", "Fleet", "Outposts"]],
      ["Observe", ["Memory", "Lessons", "Activity", "Live", "Harness"]],
    ]);
    const harness = SECTIONS[2].pages.find((p) => p.view === "harness");
    expect(harness?.desc).toBe("Which agent can review which change");
    const rules = SECTIONS[1].pages.find((p) => p.view === "rules");
    expect(rules?.desc).toBe("What may move work, and what holds each rule");
    for (const s of SECTIONS) for (const p of s.pages) expect(p.desc, p.label).not.toBe("");
  });

  it("renders the inline section in that order", async () => {
    wrap(<LeftNav hosted />, "/p/CORE/code");
    await screen.findByText("Code graph");
    const links = within(document.getElementById("nav-build")!).getAllByRole("link");
    expect(links.map((l) => l.textContent?.replace(/\d+$/, ""))).toEqual(
      ["Links", "Code graph", "Stack", "Rules", "MCP Tools", "Fleet", "Outposts"],
    );
  });
});

/** A section's inline panel is folded out: visible to the accessibility tree and 1fr tall. */
const isFolded = (key: string) => {
  const panel = document.getElementById(`nav-${key}`)!;
  return panel.getAttribute("aria-hidden") !== "true" && panel.style.gridTemplateRows === "1fr";
};
const collapsedRail = () => localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");

describe("GRPH-1222 expanded accordion", () => {
  it("on a Plan page, clicking Build folds its 7 pages out and Plan up, with no flyout", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    await screen.findByText("Tracker");
    expect(isFolded("plan")).toBe(true);
    expect(isFolded("build")).toBe(false);
    const build = expanded().getByRole("button", { name: "Build" });
    expect(build).toHaveAttribute("aria-expanded", "false");
    await user.click(build);
    expect(build).toHaveAttribute("aria-expanded", "true");
    expect(expanded().getByRole("button", { name: "Plan" })).toHaveAttribute("aria-expanded", "false");
    expect(isFolded("build")).toBe(true);
    expect(isFolded("plan")).toBe(false);
    expect(within(document.getElementById("nav-build")!).getAllByRole("link")).toHaveLength(7);
    // The project card above keeps its own switcher dialog; no section opens one.
    const nav = expanded().getByRole("navigation", { name: "Project" });
    expect(nav.querySelector("[aria-haspopup]")).toBeNull();
    expect(within(nav).queryAllByRole("dialog")).toHaveLength(0);
    expect(screen.queryAllByRole("dialog")).toHaveLength(0);
    expect(document.querySelector("[data-flyout]")).toBeNull();
    expect(screen.getByTestId("here")).toHaveTextContent("/p/CORE/tracker");
  });

  it("every section header is a disclosure button with a chevron", async () => {
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    for (const name of ["Plan", "Build", "Observe"]) {
      const header = expanded().getByRole("button", { name });
      expect(header).toHaveAttribute("aria-expanded", name === "Plan" ? "true" : "false");
      expect(header).not.toHaveAttribute("aria-haspopup");
      expect(header.querySelector('[data-chevron="section"]')).not.toBeNull();
    }
  });

  it("clicking the open section folds it up, leaving none open", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(expanded().getByRole("button", { name: "Plan" }));
    for (const key of ["plan", "build", "observe"]) expect(isFolded(key), key).toBe(false);
  });

  it("navigating from Plan to a Build page folds Build out", async () => {
    const user = userEvent.setup();
    // A link outside the sidebar, so Build is still folded up when the route changes.
    wrap(<><LeftNav /><Link to="/stack">go to stack</Link></>, "/tracker");
    await screen.findByText("Tracker");
    expect(isFolded("build")).toBe(false);
    await user.click(screen.getByRole("link", { name: "go to stack" }));
    expect(screen.getByTestId("here")).toHaveTextContent("/stack");
    expect(isFolded("build")).toBe(true);
    expect(isFolded("plan")).toBe(false);
  });

  it("lands with the route's section folded out", async () => {
    wrap(<LeftNav />, "/harness");
    await screen.findByText("Harness");
    expect(isFolded("observe")).toBe(true);
    expect(isFolded("plan")).toBe(false);
    expect(within(document.getElementById("nav-observe")!).getByRole("link", { name: "Harness" }))
      .toHaveAttribute("aria-current", "page");
  });

  it("folds with a grid-rows animation", async () => {
    stubReducedMotion(false);
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    const plan = document.getElementById("nav-plan")!;
    const build = document.getElementById("nav-build")!;
    expect(plan.style.gridTemplateRows).toBe("1fr");
    expect(build.style.gridTemplateRows).toBe("0fr");
    expect(build.style.transition).toContain("grid-template-rows 240ms cubic-bezier(.2,.8,.2,1)");
    await user.click(expanded().getByRole("button", { name: "Build" }));
    expect(build.style.gridTemplateRows).toBe("1fr");
    expect(plan.style.gridTemplateRows).toBe("0fr");
    expect(plan).toHaveAttribute("aria-hidden", "true");
  });
});

describe("GRPH-1142 rail flyout", () => {
  it("opens from a rail icon beside the sidebar with header, descriptions, counts and an esc hint, without navigating", async () => {
    const user = userEvent.setup();
    collapsedRail();
    wrap(<LeftNav hosted />, "/p/CORE/code");
    const icon = rail().getByRole("button", { name: "Plan" });
    expect(icon).toHaveAttribute("aria-haspopup", "dialog");
    await user.click(icon);
    const fly = await screen.findByRole("dialog", { name: "Plan" });
    expect(icon).toHaveAttribute("aria-expanded", "true");
    expect(fly.className).toContain("w-[264px]");
    expect(fly).toHaveTextContent("PLAN");
    expect(fly).toHaveTextContent("CORE");
    expect(within(fly).getByRole("link", { name: /Tracker/ })).toHaveTextContent("One linear stream of work");
    expect(await within(fly).findByText("41")).toBeInTheDocument();
    expect(fly).toHaveTextContent("esc to close");
    expect(screen.getByTestId("here")).toHaveTextContent("/p/CORE/code");
  });

  it("collapsed: clicking the Observe icon opens the flyout with its pages", async () => {
    const user = userEvent.setup();
    collapsedRail();
    wrap(<LeftNav />, "/tracker");
    const icon = rail().getByRole("button", { name: "Observe" });
    await user.click(icon);
    const fly = await screen.findByRole("dialog", { name: "Observe" });
    expect(within(fly).getAllByRole("link").map((l) => l.textContent?.match(/^(Memory|Lessons|Activity|Live|Harness)/)?.[1]))
      .toEqual(["Memory", "Lessons", "Activity", "Live", "Harness"]);
    expect(within(fly).getByRole("link", { name: /Live/ })).toHaveTextContent("Who is on the project right now");
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });

  it("closes on Esc and returns focus to the icon that opened it", async () => {
    const user = userEvent.setup();
    collapsedRail();
    wrap(<LeftNav />, "/tracker");
    const icon = rail().getByRole("button", { name: "Build" });
    await user.click(icon);
    await screen.findByRole("dialog", { name: "Build" });
    await user.keyboard("{Escape}");
    expect(openFlyouts()).toHaveLength(0);
    expect(icon).toHaveAttribute("aria-expanded", "false");
    expect(icon).toHaveFocus();
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });

  it("closes on an outside click, but not on a click inside it", async () => {
    const user = userEvent.setup();
    collapsedRail();
    wrap(<LeftNav />, "/tracker");
    await user.click(rail().getByRole("button", { name: "Build" }));
    const fly = await screen.findByRole("dialog", { name: "Build" });
    fireEvent.mouseDown(within(fly).getByText("esc to close"));
    expect(openFlyouts()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "outside" }));
    expect(openFlyouts()).toHaveLength(0);
  });

  it("keeps one flyout open: opening a second closes the first, and its own icon closes it", async () => {
    const user = userEvent.setup();
    collapsedRail();
    wrap(<LeftNav />, "/tracker");
    await user.click(rail().getByRole("button", { name: "Build" }));
    await screen.findByRole("dialog", { name: "Build" });
    await user.click(rail().getByRole("button", { name: "Observe" }));
    expect(openFlyouts()).toHaveLength(1);
    expect(screen.getByRole("dialog", { name: "Observe" })).toBeInTheDocument();
    expect(rail().getByRole("button", { name: "Build" })).toHaveAttribute("aria-expanded", "false");
    await user.click(rail().getByRole("button", { name: "Observe" }));
    expect(openFlyouts()).toHaveLength(0);
  });

  it("navigates on a page click, closes, and makes that section the rail's", async () => {
    const user = userEvent.setup();
    collapsedRail();
    wrap(<LeftNav />, "/tracker");
    await user.click(rail().getByRole("button", { name: "Observe" }));
    const fly = await screen.findByRole("dialog", { name: "Observe" });
    await user.click(within(fly).getByRole("link", { name: /Harness/ }));
    expect(screen.getByTestId("here")).toHaveTextContent("/harness");
    expect(openFlyouts()).toHaveLength(0);
    expect(rail().getByRole("link", { name: "Harness" })).toBeInTheDocument();
    expect(rail().getByRole("button", { name: "Plan" })).toHaveAttribute("aria-haspopup", "dialog");
  });

  it("keeps itself inside the viewport", async () => {
    const user = userEvent.setup();
    collapsedRail();
    wrap(<LeftNav />, "/tracker");
    const aside = document.querySelector<HTMLElement>('aside[aria-label="Sidebar"]')!;
    vi.spyOn(aside, "getBoundingClientRect").mockReturnValue(
      { top: 0, left: 0, right: 60, bottom: 600, width: 60, height: 600, x: 0, y: 0, toJSON() {} },
    );
    const icon = rail().getByRole("button", { name: "Observe" });
    vi.spyOn(icon, "getBoundingClientRect").mockReturnValue(
      { top: 560, left: 10, right: 50, bottom: 596, width: 40, height: 36, x: 10, y: 560, toJSON() {} },
    );
    const offset = vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(360);
    await user.click(icon);
    const fly = await screen.findByRole("dialog", { name: "Observe" });
    // 600 tall, 360 of flyout, 8px margin: it sits at 232, not at the icon's 552.
    expect(fly.style.top).toBe("232px");
    offset.mockRestore();
  });

  it("does not animate under prefers-reduced-motion", async () => {
    stubReducedMotion(true);
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    expect(document.getElementById("nav-plan")!.style.transition).toBe("none");
    expect(document.getElementById("nav-build")!.style.transition).toBe("none");
    await user.click(screen.getByRole("button", { name: /Collapse sidebar/ }));
    await user.click(rail().getByRole("button", { name: "Build" }));
    expect((await screen.findByRole("dialog", { name: "Build" })).style.transition).toBe("none");
  });
});
