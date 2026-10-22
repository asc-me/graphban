import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DETAIL_BUDGET } from "@/lib/graph/galaxy";
import type { CodeMap, CodeNode } from "@/lib/types";

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

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: current, isLoading: false, isError: false, refetch: vi.fn() }),
  useCodeAnalysis: () => ({ data: null, isLoading: false }),
  useFleetPresence: () => ({ data: null, isLoading: false }),
}));

async function mount(n: number, truncated?: boolean) {
  current = mapOf(n, truncated);
  const { CodeGraphView } = await import("@/features/code/CodeGraphView");
  render(<CodeGraphView />);
}

describe("code graph mode", () => {
  it("keeps the SVG view at the detail budget", async () => {
    expect(DETAIL_BUDGET).toBe(800);
    await mount(800);
    expect(screen.queryByTestId("code-graph-canvas")).not.toBeInTheDocument();
    expect(screen.getByRole("application").tagName.toLowerCase()).toBe("svg");
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
});
