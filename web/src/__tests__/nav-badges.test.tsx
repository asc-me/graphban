import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LeftNav, SIDEBAR_STORAGE_KEY } from "@/components/shell/LeftNav";
import { ProjectProvider } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import type { ShellCounts } from "@/lib/types";

/**
 * GRPH-1151 (PRD GRPH-P59 §D4) — every page with a badge shows its own `ShellCounts` field,
 * inline (in every folded-out section, GRPH-1222), in the rail tooltip and on rail flyout rows. Absent is not zero: loading or failed, no
 * number renders; a `0` renders only when the read succeeded.
 */

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

// Every badge field distinct from the legacy ones, so a page wired to the wrong field fails.
const COUNTS: ShellCounts = {
  items: 900, items_in_progress: 901, requests: 902, review: 7, triage_new: 903, lessons_published: 904,
  items_open: 10, requests_untriaged: 6, code_nodes: 2184, mcp_tools: 24, fleet_agents: 3, outposts: 2, live_agents: 5,
};

vi.mock("@/lib/api", () => ({
  hasSession: () => true,
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(),
    orgs: vi.fn(async () => [{ id: "org1", name: "Acme", plan: "pro", role: "owner" }]),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
  },
}));

const counts = vi.mocked(api.counts);

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

const layer = (name: "rail" | "expanded") => document.querySelector<HTMLElement>(`[data-layer="${name}"]`)!;
const row = (label: string) => within(layer("expanded")).getByRole("link", { name: new RegExp(`^${label}`) });
/** Every badge in the expanded layer, folded up or out. */
const badges = () => layer("expanded").querySelectorAll("[data-badge]");
const openBadges = () => layer("expanded").querySelectorAll('[data-inline="open"] [data-badge]');

/**
 * The row label is in the first paint. The badge is a later commit: the project
 * list resolves, then `/counts`. On a loaded runner that exceeds Testing Library's
 * 1s default, and a read of the row reports the label — expected "10", got
 * "Tracker" (GRPH-1759). The wait is real (GRPH-1536).
 */
const COUNTS_LANDED = { timeout: 10_000 };

/** The badge span for a page row. Absent until the counts read lands. */
function badgeOf(label: string): HTMLElement {
  const node = row(label).querySelector("[data-badge]");
  if (!(node instanceof HTMLElement)) throw new Error(`${label} has no badge yet`);
  return node;
}

async function shownBadge(label: string): Promise<HTMLElement> {
  return waitFor(() => {
    const node = badgeOf(label);
    expect(node).toBeInTheDocument();
    return node;
  }, COUNTS_LANDED);
}

/** Fold a section out of the expanded accordion and return its panel. */
async function fold(user: ReturnType<typeof userEvent.setup>, section: string) {
  await user.click(within(layer("expanded")).getByRole("button", { name: section }));
  const panel = document.getElementById(`nav-${section.toLowerCase()}`)!;
  await waitFor(() => expect(panel).toHaveAttribute("data-inline", "open"), COUNTS_LANDED);
  return panel;
}

afterEach(() => {
  localStorage.clear();
  counts.mockReset();
});

describe("GRPH-1151 badge counts", { timeout: 20_000 }, () => {
  it("shows each page's mapped field inline", async () => {
    counts.mockResolvedValue(COUNTS);
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    // The badge, not the row: the row's text is the label until the count commits.
    expect(await shownBadge("Tracker")).toHaveTextContent(/^10$/);
    expect(await shownBadge("Requests")).toHaveTextContent(/^6$/);
    expect(row("Triage")).not.toHaveTextContent(/\d/);
    expect(row("Specs")).not.toHaveTextContent(/\d/);
    expect(row("Triage").querySelector("[data-badge]")).toBeNull();
    expect(row("Specs").querySelector("[data-badge]")).toBeNull();
  });

  it("shows Build counts in its folded-out rows and Live green with the live suffix", async () => {
    const user = userEvent.setup();
    counts.mockResolvedValue(COUNTS);
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    expect(await shownBadge("Tracker")).toHaveTextContent(/^10$/);

    const build = await fold(user, "Build");
    expect(within(build).getByRole("link", { name: /Code graph/ })).toHaveTextContent("2,184");
    expect(within(build).getByRole("link", { name: /MCP Tools/ })).toHaveTextContent("24");
    expect(within(build).getByRole("link", { name: /Fleet/ })).toHaveTextContent("3");
    expect(within(build).getByRole("link", { name: /Outposts/ })).toHaveTextContent("2");

    const observe = await fold(user, "Observe");
    const live = within(observe).getByText("5 live");
    expect(live.className).toContain("text-st-done");
    expect(within(observe).getByRole("link", { name: /Memory/ })).toHaveTextContent("7");
  });

  it("shows counts on the collapsed rail's flyout rows", async () => {
    const user = userEvent.setup();
    localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");
    counts.mockResolvedValue(COUNTS);
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    await waitFor(() => expect(counts).toHaveBeenCalled(), COUNTS_LANDED);
    await user.click(within(layer("rail")).getByRole("button", { name: "Build" }));
    const build = await screen.findByRole("dialog", { name: "Build" }, COUNTS_LANDED);
    await waitFor(() => expect(within(build).getByRole("link", { name: /Code graph/ })).toHaveTextContent("2,184"), COUNTS_LANDED);
    expect(within(build).getByRole("link", { name: /MCP Tools/ })).toHaveTextContent("24");
  });

  it("puts the count in the rail tooltip", async () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");
    counts.mockResolvedValue(COUNTS);
    wrap(<LeftNav hosted />, "/p/CORE/live");
    const tip = document.querySelector<HTMLElement>('[role="tooltip"]')!;
    const live = within(layer("rail")).getByRole("link", { name: "Live" });
    await waitFor(() => {
      fireEvent.mouseEnter(live);
      expect(tip).toHaveTextContent("OBSERVE");
      expect(tip).toHaveTextContent("5 live");
    }, COUNTS_LANDED);
    expect(within(tip).getByText("5 live").className).toContain("text-st-done");
  });

  it("renders no badge while the counts are loading", async () => {
    counts.mockReturnValue(new Promise<ShellCounts>(() => {}));
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    await waitFor(() => expect(counts).toHaveBeenCalled(), COUNTS_LANDED);
    expect(badges()).toHaveLength(0);
    expect(row("Tracker")).not.toHaveTextContent("0");
  });

  it("renders no badge, and no 0 stand-in, after a failed read", async () => {
    const user = userEvent.setup();
    counts.mockRejectedValue(new Error("500"));
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    await waitFor(() => expect(counts).toHaveBeenCalled(), COUNTS_LANDED);
    // Let the rejection settle before asserting the absence.
    await new Promise((r) => setTimeout(r, 0));
    expect(badges()).toHaveLength(0);
    expect(row("Tracker")).not.toHaveTextContent("0");
    const observe = await fold(user, "Observe");
    expect(observe).not.toHaveTextContent(/\d/);
  });

  it("renders a real zero as 0 when the read succeeded", async () => {
    const user = userEvent.setup();
    counts.mockResolvedValue({ ...COUNTS, items_open: 0, review: 0, live_agents: 0 });
    wrap(<LeftNav hosted />, "/p/CORE/tracker");
    await waitFor(() => expect(openBadges()).toHaveLength(2), COUNTS_LANDED);
    expect(row("Tracker")).toHaveTextContent("0");
    const observe = await fold(user, "Observe");
    expect(within(observe).getByText("0 live")).toBeInTheDocument();
    expect(within(observe).getByRole("link", { name: /Memory/ })).toHaveTextContent("0");
  });
});
