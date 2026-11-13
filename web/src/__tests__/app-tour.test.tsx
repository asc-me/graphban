import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LeftNav, SIDEBAR_STORAGE_KEY } from "@/components/shell/LeftNav";
import { TopBar } from "@/components/shell/TopBar";
import { ProjectProvider } from "@/features/ProjectContext";
import { AppTour } from "@/features/tour/AppTour";
import { TOUR_STOPS, anchorHref, resolveStops, visibleAnchor } from "@/features/tour/stops";
import { api } from "@/lib/api";
import type { ShellCounts } from "@/lib/types";

/**
 * GRPH-1363 (GRPH-P67 §5) — the app tour over the real nav. Anchors are `data-tour`
 * attributes; a stop whose anchor is not rendered is skipped and the count drops with it.
 * The variant table is the ratchet: rename a nav item's `data-tour` and it fails here, not
 * silently in the tour.
 */

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

const COUNTS: ShellCounts = {
  items: 9, items_in_progress: 0, requests: 0, review: 2, triage_new: 0, lessons_published: 0,
  items_open: 9, requests_untriaged: 0, code_nodes: 120, mcp_tools: 24, fleet_agents: 1, outposts: 0, live_agents: 0,
};

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({ user: null, logout: () => {} }),
}));

vi.mock("@/lib/api", () => ({
  hasSession: () => true,
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(),
    config: vi.fn(async () => ({ hosted_mode: false })),
    onboarding: vi.fn(async () => ({ onboarding: null })),
    orgs: vi.fn(async () => [{ id: "org1", name: "Acme", plan: "pro", role: "owner" }]),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    apiKeys: vi.fn(async () => []),
    mcpTools: vi.fn(async () => ({ live: 0, tools: [] })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
    gitops: vi.fn(async () => ({ control: { state: "local" } })),
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

function Chrome({ hosted }: { hosted: boolean }) {
  return (
    <>
      <TopBar agentOpen={false} onToggleAgent={() => {}} onOpenPalette={() => {}} />
      <LeftNav hosted={hosted} />
    </>
  );
}

type Variant = { name: string; hosted: boolean; path: string; collapsed?: boolean; linked?: boolean; skipped: string[] };

/**
 * Every nav the product renders, and what the tour skips on it. Fleet v1 and v2 are the
 * Fleet page's two tabs (the old `fleet.v1` / `fleet.v2` bookmarks): one nav row either way.
 */
const VARIANTS: Variant[] = [
  { name: "hosted", hosted: true, path: "/p/CORE/tracker", skipped: [] },
  { name: "self-host", hosted: false, path: "/tracker", skipped: [] },
  { name: "hosted, Fleet v1", hosted: true, path: "/p/CORE/fleet?tab=manage", skipped: [] },
  { name: "hosted, Fleet v2", hosted: true, path: "/p/CORE/fleet?tab=harness", skipped: [] },
  { name: "self-host, Fleet v1", hosted: false, path: "/fleet?tab=manage", skipped: [] },
  { name: "self-host, Fleet v2", hosted: false, path: "/fleet?tab=harness", skipped: [] },
  { name: "hosted, collapsed rail", hosted: true, path: "/p/CORE/tracker", collapsed: true, skipped: [] },
  { name: "self-host, collapsed rail", hosted: false, path: "/home", collapsed: true, skipped: [] },
  { name: "self-host, linked", hosted: false, path: "/tracker", linked: true, skipped: [] },
];

beforeEach(() => {
  counts.mockResolvedValue(COUNTS);
});

afterEach(() => {
  localStorage.clear();
  counts.mockReset();
  vi.mocked(api.syncStatus).mockResolvedValue({
    linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
  } as Awaited<ReturnType<typeof api.syncStatus>>);
  vi.unstubAllGlobals();
});

describe("GRPH-1363 every stop resolves or is listed as skipped, per nav variant", () => {
  it.each(VARIANTS)("$name", async (v) => {
    if (v.collapsed) localStorage.setItem(SIDEBAR_STORAGE_KEY, "1");
    if (v.linked) {
      vi.mocked(api.syncStatus).mockResolvedValue({
        linked: true, source: "web", cloud_url: "https://cloud.example.com", org: "acme", credential_set: true, linked_at: null, projects: [],
      } as Awaited<ReturnType<typeof api.syncStatus>>);
    }
    wrap(<Chrome hosted={v.hosted} />, v.path);
    // Hosted needs the project's tag for its links; wait for the Dashboard row.
    await waitFor(() => expect(document.querySelector('[data-tour="dashboard"]')).not.toBeNull());
    // Self-host renders the Organization entry as a button once the sync status resolves, which is
    // what a real box shows at the tour's last stop; resolve against that, not the loading row.
    if (!v.hosted) {
      await waitFor(() => expect(document.querySelector('button[data-tour="org"][data-org-state]')).not.toBeNull());
      expect(document.querySelector('button[data-tour="org"]')).toHaveAttribute("data-org-state", v.linked ? "linked" : "unlinked");
    }
    const { resolved, skipped } = resolveStops();
    expect(skipped).toEqual(v.skipped);
    expect(resolved.map((s) => s.id)).toEqual(TOUR_STOPS.map((s) => s.id).filter((id) => !v.skipped.includes(id)));

    // And each resolved stop is shown — not only present — once the tour has navigated to it.
    // The org plug (GRPH-1364) never navigates; it must only be in the nav.
    expect(resolved.at(-1)?.id).toBe("org");
    const hrefs = resolved.filter((s) => !s.plug).map((s) => [s.id, anchorHref(s.id) ?? v.path] as const);
    for (const [id, href] of hrefs) {
      cleanup();
      wrap(<Chrome hosted={v.hosted} />, href);
      await waitFor(() => expect(visibleAnchor(id), `${v.name}: ${id} at ${href}`).not.toBeNull());
    }
  });
});

/** A nav stand-in: the engine reads `data-tour` anchors, whatever renders them. */
function FixtureNav({ omit = [] as string[] }) {
  const ids = TOUR_STOPS.map((s) => s.id).filter((id) => !omit.includes(id));
  return (
    <nav>
      {ids.map((id) =>
        id === "agent" ? (
          <button key={id} type="button" data-tour={id}>Agent</button>
        ) : (
          <a key={id} href={`/${id}`} data-tour={id}>{id}</a>
        ),
      )}
    </nav>
  );
}

function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>;
}

function tour(props: { omit?: string[]; onExit?: () => void; onComplete?: (s: string[]) => void } = {}) {
  const onExit = props.onExit ?? vi.fn();
  const onComplete = props.onComplete ?? vi.fn();
  wrap(
    <>
      <FixtureNav omit={props.omit} />
      <Where />
      <AppTour open onExit={onExit} onComplete={onComplete} />
    </>,
    "/dashboard",
  );
  return { onExit, onComplete };
}

const dialog = () => screen.getByRole("dialog", { name: /./ });
const pos = () => document.querySelector("[data-tour-pos]")!.textContent!.replace("Stop ", "");

describe("GRPH-1363 the tour engine", () => {
  it("skips Fleet without a Fleet entry, and counts n / 7", async () => {
    const onComplete = vi.fn();
    tour({ omit: ["fleet"], onComplete });
    await waitFor(() => expect(dialog()).toBeVisible());
    expect(pos()).toBe("1 / 7");
    const titles: string[] = [];
    for (let n = 1; n <= 7; n++) {
      await waitFor(() => expect(pos()).toBe(`${n} / 7`));
      titles.push(screen.getByRole("heading").textContent!);
      fireEvent.click(screen.getByRole("button", { name: "Next" }));
    }
    expect(titles).toEqual(["Dashboard", "Tracker", "Specs", "Agent sidebar", "Memory review", "Code graph", "Activity"]);
    // Then the org plug: END OF TOUR, outside the count, with Not now / Finish tour.
    await waitFor(() => expect(pos()).toBe("END OF TOUR"));
    expect(screen.getByRole("heading")).toHaveTextContent("Organization");
    fireEvent.click(screen.getByRole("button", { name: /Not now|Finish tour/ }));
    expect(onComplete).toHaveBeenCalledWith(["fleet"]);
  });

  it("plays all eight with Fleet in the nav, and records nothing skipped", async () => {
    const onComplete = vi.fn();
    tour({ onComplete });
    await waitFor(() => expect(pos()).toBe("1 / 8"));
    for (let n = 1; n < 8; n++) {
      fireEvent.click(screen.getByRole("button", { name: "Next" }));
      await waitFor(() => expect(pos()).toBe(`${n + 1} / 8`));
    }
    expect(screen.getByRole("heading")).toHaveTextContent("Activity");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(pos()).toBe("END OF TOUR"));
    fireEvent.click(screen.getByRole("button", { name: /Not now|Finish tour/ }));
    expect(onComplete).toHaveBeenCalledWith([]);
  });

  it("wraps only the anchored row: 4px above and below, 8px either side", async () => {
    // Three 29px rows 2px apart, as in the expanded nav: Tracker sits between two neighbours.
    const rect = (top: number) =>
      ({ top, left: 20, width: 180, height: 29, right: 200, bottom: top + 29, x: 20, y: top, toJSON() {} }) as DOMRect;
    const spy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const id = this.getAttribute("data-tour");
      return rect(id === "dashboard" ? 69 : id === "tracker" ? 100 : id === "prds" ? 131 : 400);
    });
    try {
      tour();
      fireEvent.click(await screen.findByRole("button", { name: "Next" }));
      await waitFor(() => expect(document.querySelector('[data-tour-spot="tracker"]')).not.toBeNull());
      const ring = document.querySelector<HTMLElement>('[data-tour-spot="tracker"]')!;
      expect([ring.style.top, ring.style.left, ring.style.width, ring.style.height]).toEqual(["96px", "12px", "196px", "37px"]);
      // Neither neighbour's label (its row's middle) is inside the ring.
      const top = parseFloat(ring.style.top), bottom = top + parseFloat(ring.style.height);
      for (const n of [69, 131]) {
        const mid = n + 29 / 2;
        expect(mid < top || mid > bottom, `neighbour at ${n}`).toBe(true);
      }
    } finally {
      spy.mockRestore();
    }
  });

  it("re-measures once fonts are ready", async () => {
    let ready!: () => void;
    const fonts = { ready: new Promise<void>((r) => (ready = r)) };
    Object.defineProperty(document, "fonts", { value: fonts, configurable: true });
    let top = 100;
    const spy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () => ({ top, left: 20, width: 180, height: 29, right: 200, bottom: top + 29, x: 20, y: top, toJSON() {} }) as DOMRect,
    );
    try {
      tour();
      await waitFor(() => expect(document.querySelector<HTMLElement>("[data-tour-spot]")?.style.top).toBe("96px"));
      top = 140; // the webfont swapped in and the row moved
      await act(async () => {
        ready();
        await fonts.ready;
      });
      await waitFor(() => expect(document.querySelector<HTMLElement>("[data-tour-spot]")!.style.top).toBe("136px"));
    } finally {
      spy.mockRestore();
      delete (document as { fonts?: unknown }).fonts;
    }
  });

  it("animates the ring, and reduced motion turns the transition off", async () => {
    tour();
    await waitFor(() => expect(document.querySelector("[data-tour-spot]")).not.toBeNull());
    expect(document.querySelector<HTMLElement>("[data-tour-spot]")!.style.transition).toContain("280ms");
    cleanup();

    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: q.includes("prefers-reduced-motion"), media: q, onchange: null,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
    }));
    tour();
    await waitFor(() => expect(document.querySelector("[data-tour-spot]")).not.toBeNull());
    expect(document.querySelector<HTMLElement>("[data-tour-spot]")!.style.transition).toBe("none");
  });

  it("is a keyboard dialog: focus on Next, Tab stays inside, Back, Esc exits", async () => {
    const onExit = vi.fn();
    tour({ onExit });
    const next = await screen.findByRole("button", { name: "Next" });
    expect(dialog()).toHaveAttribute("aria-modal", "true");
    expect(dialog()).toHaveAccessibleName("Dashboard");
    await waitFor(() => expect(next).toHaveFocus());
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    // Tab from the last control wraps to the first; Shift+Tab from the first wraps back.
    fireEvent.keyDown(next, { key: "Tab" });
    expect(screen.getByRole("button", { name: "Read more →" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "Tab", shiftKey: true });
    expect(next).toHaveFocus();

    fireEvent.click(next);
    await waitFor(() => expect(pos()).toBe("2 / 8"));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(pos()).toBe("1 / 8"));

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onExit).toHaveBeenCalled();
  });

  it("takes focus back when it lands behind the card, so Enter acts on the tour (GRPH-1552)", async () => {
    tour({});
    const next = await screen.findByRole("button", { name: "Next" });
    await waitFor(() => expect(next).toHaveFocus());
    const behind = document.createElement("button");
    document.body.appendChild(behind);
    act(() => behind.focus());
    await waitFor(() => expect(screen.getByRole("button", { name: "Next" })).toHaveFocus());
    behind.remove();
  });

  it("announces each stop to a screen reader", async () => {
    tour();
    await waitFor(() => expect(screen.getByText("Stop 1 / 8: Dashboard")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByText("Stop 2 / 8: Tracker")).toBeInTheDocument());
  });

  it("shows empty-state copy on an empty view, and none when the count is unknown", async () => {
    counts.mockResolvedValue({ ...COUNTS, items: 0 });
    tour();
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByText("Your agent's first items will appear here.")).toBeInTheDocument());
    cleanup();

    // A failed read is not an empty tracker.
    counts.mockRejectedValue(new Error("down"));
    tour();
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByRole("heading")).toHaveTextContent("Tracker"));
    expect(document.querySelector("[data-tour-empty]")).toBeNull();
  });

  it("Read more exits into the in-app docs", async () => {
    const onExit = vi.fn();
    const opened = vi.fn();
    window.addEventListener("graphban:open-docs", opened);
    try {
      tour({ onExit });
      fireEvent.click(await screen.findByRole("button", { name: "Read more →" }));
      expect(onExit).toHaveBeenCalled();
      expect(opened).toHaveBeenCalled();
    } finally {
      window.removeEventListener("graphban:open-docs", opened);
    }
  });

  it("navigates to each stop's view by following its anchor; the Agent stop stays put", async () => {
    tour();
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/dashboard"));
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/tracker"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/prds"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByRole("heading")).toHaveTextContent("Agent sidebar"));
    expect(screen.getByTestId("where")).toHaveTextContent("/prds");
  });
});
