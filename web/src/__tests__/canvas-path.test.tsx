/**
 * Canvas "Path from here" (GRPH-1075, PRD-52 D6).
 *
 * The banner, the abortable read, and Esc are one stack: pending, then armed,
 * then the selection. Pan and hover keep running while the read does.
 */

import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CodeGraphCanvas } from "@/features/code/CodeGraphCanvas";
import { SLOW_AFTER_MS, useCanvasPath } from "@/features/code/canvas/overlay";
import type { ClusterLayout } from "@/lib/graph/bigLayout";
import type { CodeMap, CodeNode, CodePath } from "@/lib/types";

const codeAnalysis = vi.hoisted(() => vi.fn());
const hit = vi.hoisted(() => vi.fn(() => "mod/n1.py" as string | null));
const layoutBox = vi.hoisted(() => ({ current: null as ClusterLayout | null }));

vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      codeAnalysis: (...args: unknown[]) => codeAnalysis(...args),
    },
  };
});

vi.mock("@/lib/graph/useGraphLayout", () => ({
  useGraphLayout: () => ({ pos: {}, pending: false, relayout: vi.fn() }),
}));

vi.mock("@/features/ProjectContext", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useProjectCtx: () => ({ activeId: "prj_1", setActiveId: vi.fn() }),
}));

vi.mock("@/lib/graph/useAreaLayout", () => ({
  useAreaLayout: () => ({
    layout: layoutBox.current,
    pending: false,
    mode: "inline" as const,
    deaths: 0,
    slow: false,
  }),
}));

vi.mock("@/lib/graph/bigLayout", async (orig) => {
  const actual = await orig<typeof import("@/lib/graph/bigLayout")>();
  return {
    ...actual,
    hitTest: (...args: unknown[]) => hit(...(args as [])),
  };
});

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: mapBox.current, isLoading: false, isError: false, refetch: vi.fn() }),
  useCodeAnalysis: () => ({ data: null, isLoading: false }),
  useFleetPresence: () => ({ data: null, isLoading: false }),
}));

const mapBox = vi.hoisted(() => ({ current: null as CodeMap | null }));

const LAYOUT: ClusterLayout = {
  ids: ["mod/n0.py", "mod/n1.py"],
  x: Float32Array.from([0, 40]),
  y: Float32Array.from([0, 0]),
  r: Float32Array.from([8, 8]),
  kinds: ["file", "file"],
  colors: ["#fff", "#fff"],
  areaOf: ["mod", "mod"],
  areas: [{ id: "mod", cx: 20, cy: 0, r: 80, color: "#111111", count: 2, label: "mod" }],
  bundles: [],
  edges: [],
  grid: { "0,0": [0, 1] },
  depth: 1,
  nodeCount: 2,
};

function mapOf(n: number): CodeMap {
  const nodes: CodeNode[] = Array.from({ length: n }, (_, i) => ({
    id: `n${i}`,
    path: `mod/n${i}.py`,
    kind: "file",
    name: `n${i}.py`,
    lang: "py",
    summary: "",
    fresh: true,
  }));
  return { nodes, edges: [], node_count: n, edge_count: 0, outbound: [] };
}

const FOUND: CodePath = {
  a: "a.py",
  b: "b.py",
  found: true,
  missing: [],
  hops: [{ src: "a.py", dst: "b.py", type: "imports", forward: true }],
};

function hookProps(over: Partial<Parameters<typeof useCanvasPath>[0]> = {}) {
  return {
    projectId: "prj_1",
    source: "a.py" as string | null,
    edgeTypes: ["imports"],
    allEdgeTypes: ["imports", "calls", "owns", "tested_by", "references"],
    onUseAllTypes: vi.fn(),
    ...over,
  };
}

function pointer(host: HTMLElement, type: "pointerdown" | "pointerup" | "pointermove", x: number, y: number) {
  act(() => {
    host.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 }),
    );
  });
}

function box(host: HTMLElement) {
  host.getBoundingClientRect = () =>
    ({
      x: 0, y: 0, left: 0, top: 0, right: 900, bottom: 560, width: 900, height: 560,
      toJSON() { return {}; },
    }) as DOMRect;
}

function mountCanvas(pathState: ComponentProps<typeof CodeGraphCanvas>["pathState"]) {
  cleanup();
  layoutBox.current = LAYOUT;
  const onSelect = vi.fn();
  const onPathTarget = vi.fn();
  const onPathEmpty = vi.fn();
  render(
    <CodeGraphCanvas
      nodes={[
        { id: "mod/n0.py", path: "mod/n0.py", kind: "file", name: "n0.py" },
        { id: "mod/n1.py", path: "mod/n1.py", kind: "file", name: "n1.py" },
      ]}
      edges={[{ src: "mod/n0.py", dst: "mod/n1.py", type: "imports" }]}
      enabledTypes={["imports", "calls", "owns", "tested_by", "references"]}
      isolate={false}
      selectedId="mod/n0.py"
      onSelect={onSelect}
      truncated={false}
      totalNodes={2}
      frameId={null}
      pathState={pathState}
      onPathTarget={onPathTarget}
      onPathEmpty={onPathEmpty}
    />,
  );
  const host = screen.getByTestId("code-graph-canvas");
  box(host);
  return { host, onSelect, onPathTarget, onPathEmpty };
}

function esc() {
  act(() => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
}

beforeEach(() => {
  hit.mockReset();
  hit.mockReturnValue("mod/n1.py");
  codeAnalysis.mockReset();
  codeAnalysis.mockImplementation(() => new Promise(() => {}));
  layoutBox.current = LAYOUT;
  mapBox.current = mapOf(801);
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
});

describe("the armed canvas", () => {
  it("shows the pick banner and a crosshair", () => {
    const { host } = mountCanvas({ picking: true, pending: false, slow: false, path: null });
    expect(screen.getByTestId("path-banner")).toHaveTextContent("Pick a target · Esc to cancel");
    expect(host.className).toMatch(/cursor-crosshair/);
  });

  it("shows the pending banner, and a drag still pans", () => {
    const { host } = mountCanvas({ picking: true, pending: true, slow: false, path: null });
    expect(screen.getByTestId("path-banner")).toHaveTextContent("Finding path… · Esc to cancel");
    expect(screen.getByTestId("path-spinner")).toBeInTheDocument();
    const before = host.getAttribute("data-camera-x");
    pointer(host, "pointerdown", 200, 200);
    pointer(host, "pointermove", 280, 200);
    pointer(host, "pointerup", 280, 200);
    expect(host.getAttribute("data-camera-x")).not.toBe(before);
  });

  it("keeps the hover tip live while a path read is pending", () => {
    const { host } = mountCanvas({ picking: true, pending: true, slow: false, path: null });
    pointer(host, "pointermove", 400, 280);
    expect(screen.getByTestId("canvas-tip")).toHaveTextContent("n1.py");
  });

  it("says still working once the wait is long", () => {
    mountCanvas({ picking: true, pending: true, slow: true, path: null });
    expect(screen.getByTestId("path-banner")).toHaveTextContent("still working");
  });

  it("sends a node click to the path and an empty click disarms", () => {
    const armed = mountCanvas({ picking: true, pending: false, slow: false, path: null });
    pointer(armed.host, "pointerdown", 300, 200);
    pointer(armed.host, "pointerup", 300, 200);
    expect(armed.onPathTarget).toHaveBeenCalledWith("mod/n1.py");
    expect(armed.onSelect).not.toHaveBeenCalled();

    hit.mockReturnValue(null);
    const empty = mountCanvas({ picking: true, pending: true, slow: false, path: null });
    pointer(empty.host, "pointerdown", 300, 200);
    pointer(empty.host, "pointerup", 301, 200);
    expect(empty.onPathEmpty).toHaveBeenCalled();
    expect(empty.onSelect).not.toHaveBeenCalled();
  });

  it("draws the path's typed edges", () => {
    const path: CodePath = {
      a: "mod/n0.py",
      b: "mod/n1.py",
      found: true,
      missing: [],
      hops: [{ src: "mod/n0.py", dst: "mod/n1.py", type: "imports", forward: true }],
    };
    mountCanvas({ picking: false, pending: false, slow: false, path });
    expect(screen.getByTestId("path-edge")).toHaveAttribute("data-edge-type", "imports");
    expect(screen.getByTestId("path-edge")).toHaveTextContent("imports");
  });
});

describe("the path request", () => {
  it("aborts on Esc, then disarms, then reports the selection layer", () => {
    const { result } = renderHook(() => useCanvasPath(hookProps()));
    act(() => {
      result.current.toggle();
      result.current.pick("b.py");
    });
    expect(result.current.pending).toBe(true);
    const signal = (codeAnalysis.mock.calls[0][0] as { signal: AbortSignal }).signal;
    let layer = "";
    act(() => {
      layer = result.current.escape();
    });
    expect(layer).toBe("pending");
    expect(signal.aborted).toBe(true);
    expect(result.current.pending).toBe(false);
    expect(result.current.picking).toBe(true);
    act(() => {
      layer = result.current.escape();
    });
    expect(layer).toBe("armed");
    expect(result.current.picking).toBe(false);
    act(() => {
      layer = result.current.escape();
    });
    expect(layer).toBe("selection");
  });

  it("a newer target aborts the one before it", async () => {
    let resolveFirst: (value: unknown) => void = () => {};
    codeAnalysis.mockImplementationOnce(
      () => new Promise((resolve) => { resolveFirst = resolve; }),
    );
    codeAnalysis.mockImplementationOnce(() => Promise.resolve({
      path: { ...FOUND, a: "a.py", b: "c.py", hops: [{ src: "a.py", dst: "c.py", type: "calls", forward: true }] },
    }));
    const { result } = renderHook(() => useCanvasPath(hookProps()));
    act(() => {
      result.current.toggle();
      result.current.pick("b.py");
    });
    const first = (codeAnalysis.mock.calls[0][0] as { signal: AbortSignal }).signal;
    await act(async () => {
      result.current.pick("c.py");
    });
    expect(first.aborted).toBe(true);
    await act(async () => {
      resolveFirst({ path: FOUND });
    });
    expect(result.current.path?.hops[0].dst).toBe("c.py");
    expect(result.current.path?.hops[0].type).toBe("calls");
  });

  it("adds still working after 10 seconds and does not give up", () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useCanvasPath(hookProps()));
      act(() => {
        result.current.toggle();
        result.current.pick("b.py");
      });
      expect(result.current.slow).toBe(false);
      act(() => {
        vi.advanceTimersByTime(SLOW_AFTER_MS);
      });
      expect(result.current.slow).toBe(true);
      expect(result.current.pending).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps no-path armed and a failed read armed", async () => {
    codeAnalysis.mockResolvedValueOnce({
      path: { a: "a.py", b: "b.py", found: false, missing: [], hops: [] },
    });
    const onUseAllTypes = vi.fn();
    const { result } = renderHook(() => useCanvasPath(hookProps({ onUseAllTypes })));
    await act(async () => {
      result.current.toggle();
      result.current.pick("b.py");
    });
    expect(result.current.trace?.found).toBe(false);
    expect(result.current.path).toBeNull();
    expect(result.current.picking).toBe(true);

    codeAnalysis.mockResolvedValueOnce({
      path: FOUND,
    });
    act(() => result.current.tryAll());
    expect(onUseAllTypes).toHaveBeenCalled();
    await act(async () => {});
    expect((codeAnalysis.mock.calls.at(-1)?.[0] as { edgeTypes: string[] }).edgeTypes).toEqual([
      "imports", "calls", "owns", "tested_by", "references",
    ]);

    codeAnalysis.mockRejectedValueOnce(new Error("down"));
    const failed = renderHook(() => useCanvasPath(hookProps()));
    await act(async () => {
      failed.result.current.toggle();
      failed.result.current.pick("b.py");
    });
    expect(failed.result.current.requestError).toBe(true);
    expect(failed.result.current.picking).toBe(true);
    expect(failed.result.current.pending).toBe(false);
  });
});

describe("Path from here on the canvas view", () => {
  async function open() {
    const { CodeGraphView } = await import("@/features/code/CodeGraphView");
    render(<CodeGraphView />);
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Find a node" }), "n0.py");
    await user.click(
      within(screen.getByRole("listbox", { name: "Matching nodes" })).getByRole("option", { name: /n0\.py/ }),
    );
    await user.click(screen.getByRole("button", { name: "Path from here" }));
    return user;
  }

  it("arms, then a target click pends, and Esc peels one layer at a time", async () => {
    await open();
    expect(screen.getByTestId("path-banner")).toHaveTextContent("Pick a target · Esc to cancel");
    expect(screen.getByTestId("code-graph-canvas").className).toMatch(/cursor-crosshair/);

    const host = screen.getByTestId("code-graph-canvas");
    box(host);
    pointer(host, "pointerdown", 300, 220);
    pointer(host, "pointerup", 300, 220);

    expect(codeAnalysis).toHaveBeenCalled();
    const sent = codeAnalysis.mock.calls[0][0] as { a: string; b: string; signal: AbortSignal };
    expect(sent.a).toBe("mod/n0.py");
    expect(sent.b).toBe("mod/n1.py");
    expect(screen.getByTestId("path-banner")).toHaveTextContent("Finding path… · Esc to cancel");

    const before = host.getAttribute("data-camera-x");
    pointer(host, "pointerdown", 200, 200);
    pointer(host, "pointermove", 260, 200);
    pointer(host, "pointerup", 260, 200);
    expect(host.getAttribute("data-camera-x")).not.toBe(before);

    esc();
    expect(sent.signal.aborted).toBe(true);
    expect(screen.getByTestId("path-banner")).toHaveTextContent("Pick a target · Esc to cancel");
    expect(screen.getByRole("button", { name: "Pick the far end…" })).toBeInTheDocument();

    esc();
    expect(screen.queryByTestId("path-banner")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Path from here" })).toBeInTheDocument();

    esc();
    expect(screen.queryByRole("button", { name: "Path from here" })).not.toBeInTheDocument();
  });

  it("shows no path with Try all edge types, and a failed read stays armed with Retry", async () => {
    codeAnalysis.mockResolvedValueOnce({
      hubs: [],
      components: [],
      path: { a: "mod/n0.py", b: "mod/n1.py", found: false, missing: [], hops: [] },
    });
    await open();
    const host = screen.getByTestId("code-graph-canvas");
    box(host);
    pointer(host, "pointerdown", 300, 220);
    pointer(host, "pointerup", 300, 220);
    await waitFor(() => {
      expect(screen.getByText("No path over the shown edge types")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Try all edge types" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pick the far end…" })).toHaveAttribute("aria-pressed", "true");

    codeAnalysis.mockRejectedValueOnce(new Error("down"));
    fireEvent.click(screen.getByRole("button", { name: "Try all edge types" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Pick the far end…" })).toHaveAttribute("aria-pressed", "true");
  });
});
