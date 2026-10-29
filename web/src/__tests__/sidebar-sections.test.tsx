import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LeftNav, SECTIONS, SIDEBAR_STORAGE_KEY } from "@/components/shell/LeftNav";
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
      ["Build", ["Code graph", "Links", "Stack", "Rules", "MCP Tools", "Fleet", "Outposts"]],
      ["Observe", ["Live", "Activity", "Memory", "Lessons", "Harness"]],
    ]);
    const harness = SECTIONS[2].pages.find((p) => p.view === "harness");
    expect(harness?.desc).toBe("Which agent can review which change");
    for (const s of SECTIONS) for (const p of s.pages) expect(p.desc, p.label).not.toBe("");
  });

  it("renders the inline section in that order", async () => {
    wrap(<LeftNav hosted />, "/p/CORE/code");
    await screen.findByText("Code graph");
    const links = within(document.getElementById("nav-build")!).getAllByRole("link");
    expect(links.map((l) => l.textContent?.replace(/\d+$/, ""))).toEqual(
      ["Code graph", "Links", "Stack", "Rules", "MCP Tools", "Fleet", "Outposts"],
    );
  });
});

describe("GRPH-1142 inline section and flyout rows", () => {
  it("on a Plan page, Plan is inline and Build and Observe open flyouts", async () => {
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    expect(document.getElementById("nav-plan")).not.toBeNull();
    expect(expanded().getByRole("button", { name: "Plan" })).toHaveAttribute("aria-expanded", "true");
    expect(expanded().getByRole("button", { name: "Plan" })).not.toHaveAttribute("aria-haspopup");
    for (const name of ["Build", "Observe"]) {
      const row = expanded().getByRole("button", { name });
      expect(row).toHaveAttribute("aria-haspopup", "dialog");
      expect(row).toHaveAttribute("aria-expanded", "false");
    }
    expect(screen.queryByText("Code graph")).toBeNull();
  });

  it("on a Build page the roles swap", async () => {
    wrap(<LeftNav />, "/code");
    await screen.findByText("Code graph");
    expect(document.getElementById("nav-build")).not.toBeNull();
    expect(document.getElementById("nav-plan")).toBeNull();
    expect(expanded().getByRole("button", { name: "Build" })).not.toHaveAttribute("aria-haspopup");
    for (const name of ["Plan", "Observe"]) {
      expect(expanded().getByRole("button", { name })).toHaveAttribute("aria-haspopup", "dialog");
    }
  });

  it("toggles the inline rows with a grid-rows animation", async () => {
    stubReducedMotion(false);
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    const panel = document.getElementById("nav-plan")!;
    expect(panel.style.gridTemplateRows).toBe("1fr");
    expect(panel.style.transition).toContain("grid-template-rows 240ms cubic-bezier(.2,.8,.2,1)");
    await user.click(expanded().getByRole("button", { name: "Plan" }));
    expect(panel.style.gridTemplateRows).toBe("0fr");
    expect(panel).toHaveAttribute("aria-hidden", "true");
  });
});

describe("GRPH-1142 flyout", () => {
  it("opens beside the sidebar with header, descriptions, counts and an esc hint, without navigating", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav hosted />, "/p/CORE/code");
    await screen.findByText("Code graph");
    const row = expanded().getByRole("button", { name: "Plan" });
    await user.click(row);
    const fly = await screen.findByRole("dialog", { name: "Plan" });
    expect(row).toHaveAttribute("aria-expanded", "true");
    expect(fly.className).toContain("w-[264px]");
    expect(fly).toHaveTextContent("PLAN");
    expect(fly).toHaveTextContent("CORE");
    expect(within(fly).getByRole("link", { name: /Tracker/ })).toHaveTextContent("One linear stream of work");
    expect(await within(fly).findByText("41")).toBeInTheDocument();
    expect(fly).toHaveTextContent("esc to close");
    expect(screen.getByTestId("here")).toHaveTextContent("/p/CORE/code");
  });

  it("closes on Esc and returns focus to the row that opened it", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    const row = expanded().getByRole("button", { name: "Build" });
    await user.click(row);
    await screen.findByRole("dialog", { name: "Build" });
    await user.keyboard("{Escape}");
    expect(openFlyouts()).toHaveLength(0);
    expect(row).toHaveAttribute("aria-expanded", "false");
    expect(row).toHaveFocus();
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });

  it("closes on an outside click, but not on a click inside it", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(expanded().getByRole("button", { name: "Build" }));
    const fly = await screen.findByRole("dialog", { name: "Build" });
    fireEvent.mouseDown(within(fly).getByText("esc to close"));
    expect(openFlyouts()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "outside" }));
    expect(openFlyouts()).toHaveLength(0);
  });

  it("keeps one flyout open: opening a second closes the first, and its own row closes it", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(expanded().getByRole("button", { name: "Build" }));
    await screen.findByRole("dialog", { name: "Build" });
    await user.click(expanded().getByRole("button", { name: "Observe" }));
    expect(openFlyouts()).toHaveLength(1);
    expect(screen.getByRole("dialog", { name: "Observe" })).toBeInTheDocument();
    expect(expanded().getByRole("button", { name: "Build" })).toHaveAttribute("aria-expanded", "false");
    await user.click(expanded().getByRole("button", { name: "Observe" }));
    expect(openFlyouts()).toHaveLength(0);
  });

  it("navigates on a page click, closes, and makes that section the inline one", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    await user.click(expanded().getByRole("button", { name: "Observe" }));
    const fly = await screen.findByRole("dialog", { name: "Observe" });
    await user.click(within(fly).getByRole("link", { name: /Harness/ }));
    expect(screen.getByTestId("here")).toHaveTextContent("/harness");
    expect(openFlyouts()).toHaveLength(0);
    expect(document.getElementById("nav-observe")).not.toBeNull();
    expect(document.getElementById("nav-plan")).toBeNull();
    expect(expanded().getByRole("button", { name: "Plan" })).toHaveAttribute("aria-haspopup", "dialog");
    expect(within(document.getElementById("nav-observe")!).getByRole("link", { name: "Harness" }))
      .toHaveAttribute("aria-current", "page");
  });

  it("opens from a rail icon too", async () => {
    const user = userEvent.setup();
    localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");
    wrap(<LeftNav />, "/tracker");
    const icon = rail().getByRole("button", { name: "Observe" });
    expect(icon).toHaveAttribute("aria-haspopup", "dialog");
    await user.click(icon);
    const fly = await screen.findByRole("dialog", { name: "Observe" });
    expect(icon).toHaveAttribute("aria-expanded", "true");
    expect(within(fly).getByRole("link", { name: /Live/ })).toHaveTextContent("Who is on the project right now");
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });

  it("keeps itself inside the viewport", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    const aside = document.querySelector<HTMLElement>('aside[aria-label="Sidebar"]')!;
    vi.spyOn(aside, "getBoundingClientRect").mockReturnValue(
      { top: 0, left: 0, right: 216, bottom: 600, width: 216, height: 600, x: 0, y: 0, toJSON() {} },
    );
    const row = expanded().getByRole("button", { name: "Observe" });
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue(
      { top: 560, left: 12, right: 204, bottom: 592, width: 192, height: 32, x: 12, y: 560, toJSON() {} },
    );
    const offset = vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(360);
    await user.click(row);
    const fly = await screen.findByRole("dialog", { name: "Observe" });
    // 600 tall, 360 of flyout, 8px margin: it sits at 232, not at the row's 552.
    expect(fly.style.top).toBe("232px");
    offset.mockRestore();
  });

  it("does not animate under prefers-reduced-motion", async () => {
    stubReducedMotion(true);
    const user = userEvent.setup();
    wrap(<LeftNav />, "/tracker");
    await screen.findByText("Tracker");
    expect(document.getElementById("nav-plan")!.style.transition).toBe("none");
    await user.click(expanded().getByRole("button", { name: "Build" }));
    expect((await screen.findByRole("dialog", { name: "Build" })).style.transition).toBe("none");
  });
});
