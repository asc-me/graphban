import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeftNav, SIDEBAR_STORAGE_KEY } from "@/components/shell/LeftNav";
import { ProjectBar } from "@/components/shell/ProjectBar";
import { summaryMeta } from "@/components/shell/ProjectSwitcher";
import { ProjectProvider } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import type { ProjectSummary } from "@/lib/types";

/**
 * GRPH-1143 — one searchable switcher behind the sidebar card, the rail tile and the
 * breadcrumb: filter by name or tag, ↑↓ and ↵, and a meta line that is blank rather than
 * invented when the GRPH-1131 summary read has not answered.
 */

const base = {
  visibility: "private", description: "", share_global_memory: false, auto_extract: true,
  mcp_enabled: true, embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};
const projects = [
  { ...base, id: "core", tag: "CORE", name: "Core", accent: "#c6f24e" },
  { ...base, id: "docs", tag: "DOCS", name: "Docs site", accent: "#5fd07a" },
  { ...base, id: "ledg", tag: "LDGR", name: "AgentLedger", accent: "#7ca2ff" },
];
const recent = new Date().toISOString();
const summaries: ProjectSummary[] = [
  { id: "core", name: "Core", tag: "CORE", accent: "#c6f24e", item_count: 24, agent_count: 3, live_agents: 2, default_branch: "main", last_activity_at: recent },
  // No default branch: the meta line has no branch segment and no placeholder.
  { id: "docs", name: "Docs site", tag: "DOCS", accent: "#5fd07a", item_count: 4, agent_count: 1, live_agents: 1, default_branch: null, last_activity_at: recent },
  { id: "ledg", name: "AgentLedger", tag: "LDGR", accent: "#7ca2ff", item_count: 61, agent_count: 1, live_agents: 0, default_branch: "release/0.9", last_activity_at: recent },
];

vi.mock("@/lib/api", () => ({
  hasSession: () => true,
  api: {
    projects: vi.fn(),
    projectSummaries: vi.fn(),
    counts: vi.fn(async () => ({ items: 1, items_in_progress: 0, requests: 0, review: 0 })),
    orgs: vi.fn(async () => [{ id: "org1", name: "Ascme Labs", plan: "pro", role: "owner" }]),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    config: vi.fn(async () => ({ hosted_mode: true, signup_mode: "closed" })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
  },
}));

function Where() {
  const { pathname } = useLocation();
  return <div data-testid="where">{pathname}</div>;
}

function renderAt(path: string, ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          {ui}
          <Where />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openFromBreadcrumb(path = "/p/CORE/tracker") {
  const user = userEvent.setup();
  renderAt(path, <ProjectBar />);
  await user.click(await screen.findByRole("button", { name: "Switch project" }));
  const dialog = await screen.findByRole("dialog", { name: "Switch project" });
  return { user, dialog };
}

const names = (dialog: HTMLElement) =>
  within(dialog).queryAllByRole("option").map((o) => within(o).getByTestId("switcher-name").textContent);

beforeEach(() => {
  vi.mocked(api.projects).mockReset().mockResolvedValue(projects as never);
  vi.mocked(api.projectSummaries).mockReset().mockResolvedValue(summaries);
});

describe("project switcher popover", () => {
  it("is a dialog with a listbox, the current project selected, and the input focused", async () => {
    const { dialog } = await openFromBreadcrumb();
    const list = within(dialog).getByRole("listbox");
    const opts = within(list).getAllByRole("option");
    expect(opts).toHaveLength(3);
    expect(opts.filter((o) => o.getAttribute("aria-selected") === "true")).toHaveLength(1);
    expect(opts[0]).toHaveAttribute("aria-selected", "true");
    expect(opts[1]).toHaveAttribute("aria-selected", "false");
    expect(within(dialog).getByPlaceholderText("Find a project…")).toHaveFocus();
    expect(within(dialog).getByText("YOUR PROJECTS · ASCME LABS")).toBeInTheDocument();
    expect(within(dialog).getByText("3 OF 3")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "All projects" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /New project/ })).toBeInTheDocument();
  });

  it("filters by name or tag and says so when nothing matches", async () => {
    const { user, dialog } = await openFromBreadcrumb();
    const input = within(dialog).getByPlaceholderText("Find a project…");
    await user.type(input, "ledger");
    expect(names(dialog)).toEqual(["AgentLedger"]);
    expect(within(dialog).getByText("1 OF 3")).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "docs");
    expect(names(dialog)).toEqual(["Docs site"]);
    await user.clear(input);
    // Tag only — "LDGR" is not in any name.
    await user.type(input, "ldgr");
    expect(names(dialog)).toEqual(["AgentLedger"]);
    await user.clear(input);
    await user.type(input, "zzz");
    expect(within(dialog).queryAllByRole("option")).toHaveLength(0);
    expect(within(dialog).getByText("No projects match “zzz”.")).toBeInTheDocument();
  });

  it("moves the highlight with ↑↓ and switches with ↵, keeping the page", async () => {
    const { user, dialog } = await openFromBreadcrumb("/p/CORE/tracker");
    const opts = () => within(dialog).getAllByRole("option");
    expect(opts()[0]).toHaveAttribute("data-highlighted", "true");
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(opts()[2]).toHaveAttribute("data-highlighted", "true");
    await user.keyboard("{ArrowDown}");
    expect(opts()[2]).toHaveAttribute("data-highlighted", "true");
    await user.keyboard("{ArrowUp}");
    expect(opts()[1]).toHaveAttribute("data-highlighted", "true");
    // Selected is the current project, not the highlight.
    expect(opts()[0]).toHaveAttribute("aria-selected", "true");
    expect(opts()[1]).toHaveAttribute("aria-selected", "false");
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("where")).toHaveTextContent("/p/DOCS/tracker");
    expect(screen.queryByRole("dialog", { name: "Switch project" })).not.toBeInTheDocument();
  });

  it("↵ after filtering switches to the filtered project", async () => {
    const { user, dialog } = await openFromBreadcrumb("/p/CORE/code/x");
    await user.type(within(dialog).getByPlaceholderText("Find a project…"), "agent{Enter}");
    expect(screen.getByTestId("where")).toHaveTextContent("/p/LDGR/code/x");
  });

  it("hover moves the highlight and a click switches", async () => {
    const { user, dialog } = await openFromBreadcrumb();
    const opts = within(dialog).getAllByRole("option");
    await user.hover(opts[2]);
    expect(opts[2]).toHaveAttribute("data-highlighted", "true");
    expect(opts[0]).not.toHaveAttribute("data-highlighted");
    await user.click(opts[2]);
    expect(screen.getByTestId("where")).toHaveTextContent("/p/LDGR/tracker");
  });

  it("Esc closes and gives focus back to the trigger", async () => {
    const { user } = await openFromBreadcrumb();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Switch project" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch project" })).toHaveFocus();
  });

  it("draws the meta line and live count from the summary read", async () => {
    const { dialog } = await openFromBreadcrumb();
    await waitFor(() => expect(within(dialog).getByText("24 items · 3 agents · main")).toBeInTheDocument());
    const docs = within(dialog).getAllByRole("option")[1];
    // No branch: no trailing segment, no placeholder.
    expect(within(docs).getByTestId("switcher-meta").textContent).toBe("4 items · 1 agent");
    expect(within(dialog).getByText("2 live")).toBeInTheDocument();
    expect(within(dialog).getByText("1 live")).toBeInTheDocument();
    expect(api.projectSummaries).toHaveBeenCalledTimes(1);
  });

  it("lists every project with a blank meta line when the summary read fails", async () => {
    vi.mocked(api.projectSummaries).mockRejectedValue(new Error("500"));
    const { dialog } = await openFromBreadcrumb();
    await waitFor(() => expect(api.projectSummaries).toHaveBeenCalled());
    // Let the rejection settle before reading the rows.
    await new Promise((r) => setTimeout(r, 0));
    const opts = within(dialog).getAllByRole("option");
    expect(opts).toHaveLength(3);
    for (const o of opts) expect(within(o).getByTestId("switcher-meta").textContent).toBe("");
    expect(within(dialog).queryByText(/live$/)).not.toBeInTheDocument();
  });

  it("lists every project with a blank meta line while the summary read is loading", async () => {
    vi.mocked(api.projectSummaries).mockReturnValue(new Promise(() => {}));
    const { dialog } = await openFromBreadcrumb();
    const opts = within(dialog).getAllByRole("option");
    expect(opts).toHaveLength(3);
    for (const o of opts) expect(within(o).getByTestId("switcher-meta").textContent).toBe("");
  });

  it("does not read summaries until someone opens it", async () => {
    renderAt("/p/CORE/tracker", <ProjectBar />);
    await screen.findByRole("button", { name: "Switch project" });
    expect(api.projectSummaries).not.toHaveBeenCalled();
  });

  it("opens from the sidebar card below it and from the rail tile beside the sidebar", async () => {
    const user = userEvent.setup();
    renderAt("/p/CORE/tracker", <LeftNav hosted />);
    const expanded = (await screen.findByRole("complementary", { name: "Sidebar" })).querySelector('[data-layer="expanded"]') as HTMLElement;
    const card = await within(expanded).findByRole("button", { name: "Switch project" });
    expect(card).toHaveAttribute("aria-haspopup", "dialog");
    await user.click(card);
    expect(card).toHaveAttribute("aria-expanded", "true");
    const dialog = screen.getByRole("dialog", { name: "Switch project" });
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByTestId("where")).toHaveTextContent("/p/DOCS/tracker");
    expect(dialog).not.toBeInTheDocument();
  });

  it("opens from the rail tile and suppresses the rail label while open", async () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");
    try {
      const user = userEvent.setup();
      renderAt("/p/CORE/tracker", <LeftNav hosted />);
      const rail = (await screen.findByRole("complementary", { name: "Sidebar" })).querySelector('[data-layer="rail"]') as HTMLElement;
      const tile = await within(rail).findByRole("button", { name: "Switch project" });
      const tooltip = screen.getByRole("tooltip", { hidden: true });
      await user.hover(tile);
      expect(tooltip.style.opacity).toBe("1");
      await user.click(tile);
      const dialog = screen.getByRole("dialog", { name: "Switch project" });
      expect(within(dialog).getAllByRole("option")).toHaveLength(3);
      // Another rail icon would normally label itself; not while the switcher is open.
      await user.hover(within(rail).getByRole("link", { name: "Dashboard" }));
      expect(tooltip.style.opacity).toBe("0");
      await user.keyboard("{Escape}");
      expect(dialog).not.toBeInTheDocument();
      expect(tile).toHaveFocus();
    } finally {
      localStorage.removeItem(SIDEBAR_STORAGE_KEY);
    }
  });
});

describe("summaryMeta", () => {
  const now = Date.parse("2026-10-28T12:00:00Z");
  const row = (over: Partial<ProjectSummary>): ProjectSummary => ({
    id: "x", name: "X", tag: "X", accent: "#fff", item_count: 0, agent_count: 0, live_agents: 0,
    default_branch: null, last_activity_at: null, ...over,
  });

  it("is blank with no summary rather than a guessed zero", () => {
    expect(summaryMeta(undefined, now)).toBe("");
  });

  it("names items, agents and branch, singular where it is one", () => {
    expect(summaryMeta(row({ item_count: 1, agent_count: 1, default_branch: "main", last_activity_at: "2026-10-28T11:00:00Z" }), now))
      .toBe("1 item · 1 agent · main");
  });

  it("drops the branch segment when there is no default branch", () => {
    expect(summaryMeta(row({ item_count: 9, agent_count: 2, last_activity_at: "2026-10-28T11:00:00Z" }), now))
      .toBe("9 items · 2 agents");
  });

  it("says idle Nd once nobody has touched it for a day and nobody is live", () => {
    expect(summaryMeta(row({ item_count: 4, agent_count: 1, default_branch: "main", last_activity_at: "2026-10-16T12:00:00Z" }), now))
      .toBe("4 items · idle 12d");
  });

  it("is not idle while an agent is live", () => {
    expect(summaryMeta(row({ item_count: 4, agent_count: 1, live_agents: 1, default_branch: "main", last_activity_at: "2026-10-16T12:00:00Z" }), now))
      .toBe("4 items · 1 agent · main");
  });

  it("does not call a project that never had activity idle", () => {
    expect(summaryMeta(row({ item_count: 0 }), now)).toBe("0 items");
  });
});
