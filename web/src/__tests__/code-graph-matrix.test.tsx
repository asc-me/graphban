import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";

import {
  AreaMatrix,
  CellInspector,
  CYCLE_NOTE,
  REACHING_UP,
  SUPPORT_NOTE,
  buildMatrix,
} from "@/features/code/AreaMatrix";
import type { CodeEdge, CodeMap, CodeNode } from "@/lib/types";

/**
 * Matrix is DOM (PRD-52 D3/D7). The model tests plant the back-edge directly.
 * The view tests go through the layout chip, which only exists in canvas mode
 * (more than 800 nodes). Twelve prefixes stay twelve areas at depth 1.
 */

const FILES = 8;

function files(prefix: string, n = FILES) {
  return Array.from({ length: n }, (_, i) => ({
    id: `${prefix}/f${i}.py`,
    path: `${prefix}/f${i}.py`,
    kind: "file",
  }));
}

const smallNodes = ["data", "mid", "top", "docs"].flatMap((p) => files(p));

const smallEdges = [
  { src: "mid/f0.py", dst: "data/f0.py", type: "imports" },
  { src: "top/f0.py", dst: "mid/f0.py", type: "imports" },
  { src: "top/f1.py", dst: "mid/f0.py", type: "imports" },
  { src: "top/f2.py", dst: "mid/f0.py", type: "imports" },
  { src: "top/f3.py", dst: "mid/f0.py", type: "imports" },
  { src: "data/f0.py", dst: "top/f0.py", type: "references" },
  { src: "docs/f0.py", dst: "top/f0.py", type: "references" },
];

function renderMatrix(onSelect = vi.fn()) {
  render(
    <AreaMatrix
      nodes={smallNodes}
      edges={smallEdges}
      selected={null}
      onSelect={onSelect}
      onLayout={() => {}}
    />,
  );
  return onSelect;
}

describe("matrix model", () => {
  it("names a below-diagonal reference from a lower layer as reaching up", () => {
    expect(REACHING_UP).toBe("lower layer reaching up — worth a look");
    const model = buildMatrix(
      [
        { id: "low", members: ["low"] },
        { id: "mid", members: ["mid"] },
        { id: "high", members: ["high"] },
      ],
      [
        { src: "mid", dst: "low", type: "imports" },
        { src: "high", dst: "mid", type: "imports" },
        { src: "low", dst: "high", type: "references" },
      ],
    );
    const cell = model.cells.flat().find((c) => c.row === "low" && c.col === "high");
    expect(model.order.map((a) => a.tag)).toEqual(["L2", "L1", "L0"]);
    expect(cell?.below).toBe(true);
    expect(cell?.tone).toBe("warn");
    expect(cell?.note).toBe(REACHING_UP);
    expect(cell?.fill).toBeCloseTo(1);
  });

  it("keeps a cycle note when the cell sits below the diagonal", () => {
    const model = buildMatrix(
      [
        { id: "a", members: ["a"] },
        { id: "b", members: ["b"] },
      ],
      [
        { src: "a", dst: "b", type: "imports" },
        { src: "b", dst: "a", type: "imports" },
      ],
    );
    const below = model.cells.flat().find((c) => c.below && c.count > 0);
    expect(below?.tone).toBe("cycle");
    expect(below?.note).toBe(CYCLE_NOTE);
    expect(below?.note).not.toBe(REACHING_UP);
  });

  it("sorts support last and greys a reference that points up by design", () => {
    const model = buildMatrix(
      [
        { id: "app", members: ["app"] },
        { id: "lib", members: ["lib"] },
        { id: "docs", members: ["docs"] },
      ],
      [
        { src: "app", dst: "lib", type: "imports" },
        { src: "docs", dst: "app", type: "references" },
      ],
    );
    expect(model.order.map((a) => a.id)).toEqual(["app", "lib", "docs"]);
    expect(model.order.map((a) => a.tag)).toEqual(["L1", "L0", "support"]);
    const cell = model.cells.flat().find((c) => c.row === "docs" && c.col === "app");
    expect(cell?.below).toBe(true);
    expect(cell?.tone).toBe("neutral");
    expect(cell?.note).toBe(SUPPORT_NOTE);
  });

  it("sqrt-scales fill against the busiest cell", () => {
    const model = buildMatrix(
      [
        { id: "a", members: ["a"] },
        { id: "b", members: ["b1", "b2", "b3", "b4"] },
      ],
      [
        { src: "a", dst: "b1", type: "imports" },
        { src: "b2", dst: "a", type: "imports" },
        { src: "b3", dst: "a", type: "imports" },
        { src: "b4", dst: "a", type: "imports" },
        { src: "b1", dst: "a", type: "imports" },
      ],
    );
    const busy = model.cells.flat().find((c) => c.count === model.maxCount);
    const one = model.cells.flat().find((c) => c.count === 1);
    expect(busy?.fill).toBeCloseTo(1);
    expect(one?.fill).toBeCloseTo(0.5);
  });

  it("caps the edge list at 40", () => {
    const members = Array.from({ length: 41 }, (_, i) => `s${i}`);
    const model = buildMatrix(
      [
        { id: "src", members },
        { id: "dst", members: ["d0"] },
      ],
      members.map((src) => ({ src, dst: "d0", type: "imports" as const })),
    );
    const cell = model.cells.flat().find((c) => c.count === 41);
    expect(cell).toBeTruthy();
    render(<CellInspector cell={cell!} onShowInGraph={() => {}} onJump={() => {}} />);
    expect(screen.getAllByTestId("cell-edge")).toHaveLength(40);
    expect(screen.getByTestId("cell-more")).toHaveTextContent("+ 1 more");
  });
});

describe("matrix grid", () => {
  it("tags rows, separates bands, and puts support last with a neutral cell", () => {
    renderMatrix();
    expect(screen.getByTestId("matrix-tag-top")).toHaveTextContent("L2");
    expect(screen.getByTestId("matrix-tag-mid")).toHaveTextContent("L1");
    expect(screen.getByTestId("matrix-tag-data")).toHaveTextContent("L0");
    expect(screen.getByTestId("matrix-tag-docs")).toHaveTextContent("support");
    expect(
      screen.getAllByTestId("matrix-separator").map((row) => row.getAttribute("data-band")),
    ).toEqual(["L1", "L0", "support"]);
    const rows = screen.getAllByTestId(/^matrix-row-/);
    expect(rows[rows.length - 1]).toHaveAttribute("data-testid", "matrix-row-docs");
    const supportCell = screen.getByTestId("matrix-cell-docs-top");
    expect(supportCell).toHaveAttribute("data-tone", "neutral");
    expect((supportCell as HTMLElement).style.background).toBe("rgb(139, 148, 158)");
    const back = screen.getByTestId("matrix-cell-data-top");
    expect(back).toHaveAttribute("data-diag", "below");
    expect(back).toHaveAttribute("data-tone", "warn");
    expect(back).toHaveAttribute("data-fill", "0.5000");
    expect((back as HTMLElement).style.background).toBe("rgb(255, 143, 143)");
  });

  it("leaves zero cells unfocusable and ignores clicks", async () => {
    const user = userEvent.setup();
    const onSelect = renderMatrix();
    const zero = screen.getByTestId("matrix-cell-data-mid");
    expect(zero.tagName).toBe("SPAN");
    expect(zero).not.toHaveAttribute("tabindex");
    await user.click(zero);
    expect(onSelect).not.toHaveBeenCalled();
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(document.activeElement).not.toBe(zero);
    }
  });

  it("reaches non-zero cells as buttons in row-major order", () => {
    renderMatrix();
    const buttons = within(screen.getByTestId("area-matrix"))
      .getAllByRole("button")
      .filter((el) => el.getAttribute("data-testid")?.startsWith("matrix-cell-"));
    expect(buttons.map((el) => `${el.getAttribute("data-row")}→${el.getAttribute("data-col")}`)).toEqual([
      "top→mid",
      "mid→data",
      "data→top",
      "docs→top",
    ]);
  });

  it("restores the scroller from the saved offset", () => {
    const { rerender } = render(
      <AreaMatrix
        nodes={smallNodes}
        edges={smallEdges}
        selected={null}
        onSelect={() => {}}
        onLayout={() => {}}
        restoreScroll={0}
      />,
    );
    rerender(
      <AreaMatrix
        nodes={smallNodes}
        edges={smallEdges}
        selected={null}
        onSelect={() => {}}
        onLayout={() => {}}
        restoreScroll={140}
      />,
    );
    expect(screen.getByTestId("matrix-scroll").scrollTop).toBe(140);
  });
});

vi.mock("@/lib/graph/useGraphLayout", () => ({
  useGraphLayout: () => ({ pos: {}, pending: false, relayout: vi.fn() }),
}));

vi.mock("@/features/ProjectContext", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useProjectCtx: () => ({ activeId: "prj_1", setActiveId: vi.fn() }),
}));

const PER_AREA = 67;
const PREFIXES = Array.from({ length: 12 }, (_, i) => `p${String(i).padStart(2, "0")}`);

function bigMap(): CodeMap {
  const nodes: CodeNode[] = PREFIXES.flatMap((prefix) =>
    Array.from({ length: PER_AREA }, (_, i) => {
      const id = `${prefix}/f${i}.py`;
      return { id, path: id, kind: "file", name: `f${i}.py`, lang: "py", summary: "", fresh: true };
    }),
  );
  const edges: CodeEdge[] = [
    { src: "p01/f0.py", dst: "p00/f0.py", type: "imports" },
    { src: "p02/f0.py", dst: "p01/f0.py", type: "imports" },
    { src: "p00/f0.py", dst: "p02/f0.py", type: "references" },
    { src: "p11/f0.py", dst: "p02/f0.py", type: "references" },
  ];
  return { nodes, edges, node_count: nodes.length, edge_count: edges.length, outbound: [] };
}

let current: CodeMap = bigMap();

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: current, isLoading: false, isError: false, refetch: vi.fn() }),
  useCodeAnalysis: () => ({ data: null, isLoading: false }),
  useFleetPresence: () => ({ data: null, isLoading: false }),
}));

function zoomText() {
  return within(screen.getByTestId("graph-bottom-bar")).getByText(/\d+%/).textContent ?? "";
}

async function mountView() {
  current = bigMap();
  const { CodeGraphView } = await import("@/features/code/CodeGraphView");
  render(<CodeGraphView />);
  await waitFor(() => expect(screen.getByTestId("code-graph-canvas")).toBeInTheDocument());
}

describe("matrix in the code graph", () => {
  beforeAll(() => {
    window.matchMedia = ((query: string) => ({
      matches: query.includes("reduce"),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  });

  it("shows the back-edge, isolates that pair, and restores the cell", async () => {
    const user = userEvent.setup();
    await mountView();
    await user.click(screen.getByTestId("layout-chip-matrix"));

    expect(screen.getByTestId("matrix-tag-p02")).toHaveTextContent("L2");
    expect(screen.getByTestId("matrix-tag-p11")).toHaveTextContent("support");
    const rows = screen.getAllByTestId(/^matrix-row-/);
    expect(rows[rows.length - 1]).toHaveAttribute("data-testid", "matrix-row-p11");
    expect(screen.getByTestId("matrix-cell-p11-p02")).toHaveAttribute("data-tone", "neutral");

    const backEdge = screen.getByTestId("matrix-cell-p00-p02");
    expect(backEdge).toHaveAttribute("data-diag", "below");
    await user.click(backEdge);
    expect(within(screen.getByTestId("graph-aside")).getByText(REACHING_UP)).toBeInTheDocument();

    const scroller = screen.getByTestId("matrix-scroll");
    scroller.scrollTop = 180;

    await user.click(screen.getByRole("button", { name: "Show in graph" }));
    expect(screen.getByTestId("layout-chip-clusters")).toHaveAttribute("aria-pressed", "true");
    const isolate = screen.getByRole("button", { name: "Isolate" });
    expect(isolate).toBeEnabled();
    expect(isolate).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-pair-edges", "1"));
    expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-pair", "p00→p02");
    expect(screen.getByTestId("back-to-matrix")).toHaveTextContent("Back to matrix · p00→p02");

    await user.click(screen.getByTestId("back-to-matrix"));
    expect(screen.getByTestId("matrix-cell-p00-p02")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("matrix-scroll").scrollTop).toBe(180);
    expect(within(screen.getByTestId("graph-aside")).getByText(REACHING_UP)).toBeInTheDocument();

    await user.click(screen.getByTestId("layout-chip-matrix"));
    expect(screen.getByTestId("matrix-cell-p00-p02")).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByTestId("cell-inspector")).not.toBeInTheDocument();
  });

  it("lists every area and frames the one that is clicked", async () => {
    const user = userEvent.setup();
    await mountView();
    const aside = screen.getByTestId("graph-aside");
    const jumps = within(aside)
      .getAllByRole("button")
      .filter((el) => el.textContent?.includes(`${PER_AREA} nodes`));
    expect(jumps).toHaveLength(PREFIXES.length);
    for (const prefix of PREFIXES) {
      expect(within(aside).getByRole("button", { name: new RegExp(prefix) })).toHaveTextContent(
        `${PER_AREA} nodes`,
      );
    }
    const before = zoomText();
    await user.click(within(aside).getByRole("button", { name: /p00/ }));
    expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-frame-area", "p00");
    expect(within(aside).getByText("Explore")).toBeInTheDocument();
    expect(within(aside).queryByRole("button", { name: /Path from here/ })).not.toBeInTheDocument();
    await waitFor(() => expect(zoomText()).not.toBe(before));
  });
});
