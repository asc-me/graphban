import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DETAIL_BUDGET } from "@/lib/graph/galaxy";
import type { CodeMap, CodeNode, FleetPresence } from "@/lib/types";

/**
 * Mode follows the node count (PRD-52 D1). The force layout is mocked so an
 * 800-node SVG case does not run it. Canvas mode passes an empty id list to
 * that hook; this mock makes the assertion independent of that short-circuit.
 */
vi.mock("@/lib/graph/useGraphLayout", () => ({
  useGraphLayout: () => ({ pos: {}, pending: false, relayout: vi.fn() }),
}));

vi.mock("@/features/ProjectContext", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useProjectCtx: () => ({ activeId: "prj_1", setActiveId: vi.fn() }),
}));

function mapOf(n: number, truncated?: boolean): CodeMap {
  const nodes: CodeNode[] = Array.from({ length: n }, (_, i) => ({
    id: `n${i}`,
    path: `mod/n${i}.py`,
    kind: "file",
    name: `n${i}.py`,
    lang: "py",
    summary: "",
    fresh: true,
  }));
  return {
    nodes,
    edges: [],
    node_count: n,
    edge_count: 0,
    outbound: [],
    ...(truncated === undefined ? {} : { truncated }),
  };
}

let current: CodeMap = mapOf(3);
let presenceData: FleetPresence | null = null;

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: current, isLoading: false, isError: false, refetch: vi.fn() }),
  useCodeAnalysis: () => ({ data: null, isLoading: false, isFetching: false }),
  useFleetPresence: () => ({ data: presenceData, isLoading: false }),
}));

beforeEach(() => {
  presenceData = null;
});

async function mount(n: number, truncated?: boolean) {
  current = mapOf(n, truncated);
  const { CodeGraphView } = await import("@/features/code/CodeGraphView");
  render(<CodeGraphView />);
}

describe("code graph mode", () => {
  it("renders canvas mode for a small map too", async () => {
    // The SVG view below 800 opened on an empty frame, and its Fit only reset to 100%.
    expect(DETAIL_BUDGET).toBe(0);
    await mount(3);
    expect(screen.getByTestId("code-graph-canvas")).toBeInTheDocument();
    expect(screen.getAllByRole("application")).toHaveLength(1);
  });

  it("renders canvas mode above the detail budget", async () => {
    await mount(801);
    expect(screen.getByTestId("code-graph-canvas")).toBeInTheDocument();
    expect(screen.getAllByRole("application")).toHaveLength(1);
  });

  it("shows the truncated caption when the payload says so", async () => {
    await mount(801, true);
    expect(screen.getByTestId("truncated-caption")).toHaveTextContent(/truncated/);
  });

  it("keeps Isolate on the control cluster in canvas mode", async () => {
    // The edge cap tells the reader to Isolate. Hiding GraphControls above the
    // detail budget leaves that instruction with no control. The button lives
    // in the bottom bar, and a selection is what arms it.
    const user = userEvent.setup();
    await mount(801);
    const bar = screen.getByTestId("graph-bottom-bar");
    const isolate = within(bar).getByRole("button", { name: "Isolate" });
    expect(isolate).toBeDisabled();

    await user.type(screen.getByRole("textbox", { name: "Find a node" }), "n800.py");
    await user.click(
      within(screen.getByRole("listbox", { name: "Matching nodes" })).getByRole("option", {
        name: /n800\.py/,
      }),
    );

    expect(isolate).toBeEnabled();
    await user.click(isolate);
    expect(isolate).toHaveAttribute("aria-pressed", "true");
  });

  it("announces the holder the presence read actually returned", async () => {
    presenceData = {
      served_at: "2026-10-22T00:00:00Z",
      heartbeat_interval_seconds: 50,
      held: [{
        area: "mod",
        item_id: null,
        expires_at: null,
        agent_id: "a",
        agent_label: "Ada",
        active_role: "worker",
        state: "working",
        user_id: "u",
        user_initials: "AD",
        user_color: "#fff",
        node_paths: ["mod/n0.py"],
      }],
      off_map: [],
      truncated: false,
      total: 1,
    };
    await mount(801);
    const list = screen.getByRole("listbox", { name: "Graph nodes" });
    act(() => {
      list.focus();
      list.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true }));
    });
    expect(screen.getByTestId("graph-live")).toHaveTextContent("File mod/n0.py, 0 connections, held by AD");
    expect(screen.getByRole("application").getAttribute("aria-label")).toMatch(/801 nodes, 0 edges/);
  });

  it("keeps solo and the off-map tray in the canvas-mode bottom bar", async () => {
    const user = userEvent.setup();
    presenceData = {
      served_at: "2026-10-23T00:00:00Z",
      heartbeat_interval_seconds: 50,
      held: [{
        area: "mod",
        item_id: null,
        expires_at: null,
        agent_id: "a",
        agent_label: "Ada",
        active_role: "worker",
        state: "working",
        user_id: "u",
        user_initials: "AD",
        user_color: "#c6f24e",
        node_paths: ["mod/n0.py"],
      }],
      off_map: [{
        area: "vercel env",
        item_id: null,
        expires_at: null,
        agent_id: "b",
        agent_label: "Bea",
        active_role: "worker",
        state: "working",
        user_id: "u2",
        user_initials: "BE",
        user_color: "#7ca2ff",
        reason: "undescribed",
      }],
      truncated: false,
      total: 2,
    };
    await mount(801);
    expect(screen.getByTestId("code-graph-canvas")).toBeInTheDocument();
    const bar = screen.getByTestId("graph-bottom-bar");
    expect(within(bar).getByRole("button", { name: "fleet" })).toBeInTheDocument();
    const chip = within(bar).getByRole("button", { name: /1a · 1n/ });
    await user.click(chip);
    expect(chip).toHaveAttribute("aria-pressed", "true");
    const tray = within(bar).getByRole("button", { name: /not on this map/ });
    await user.click(tray);
    expect(within(bar).getByText("vercel env")).toBeInTheDocument();
    expect(within(bar).getByText("undescribed")).toBeInTheDocument();
    expect(chip).toHaveAttribute("aria-pressed", "true");
  });

  it("Escape disarms path mode before it clears the selection", async () => {
    const user = userEvent.setup();
    await mount(801);
    const bar = screen.getByTestId("graph-bottom-bar");
    const isolate = within(bar).getByRole("button", { name: "Isolate" });
    await user.type(screen.getByRole("textbox", { name: "Find a node" }), "n800.py");
    await user.click(
      within(screen.getByRole("listbox", { name: "Matching nodes" })).getByRole("option", {
        name: /n800\.py/,
      }),
    );
    await user.click(screen.getByRole("button", { name: "Path from here" }));
    expect(screen.getByRole("button", { name: "Pick the far end…" })).toHaveAttribute("aria-pressed", "true");

    const list = screen.getByRole("listbox", { name: "Graph nodes" });
    act(() => list.focus());
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Path from here" })).toHaveAttribute("aria-pressed", "false");
    expect(isolate).toBeEnabled();

    await user.keyboard("{Escape}");
    expect(isolate).toBeDisabled();
  });
});
