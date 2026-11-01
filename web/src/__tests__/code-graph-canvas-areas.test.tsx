import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { bundlesFromEdges, bundleStrokes, quadPoint } from "@/features/code/canvas/edges";
import type { ClusterLayout, RankedEdge } from "@/lib/graph/bigLayout";

const layoutBox = vi.hoisted(() => ({
  current: null as ClusterLayout | null,
}));

/**
 * A 12,000-node map draws areas and bundles only. The one file sits inside
 * hull A, which the fitted camera does not open, so a click there must not
 * select it. `nodeCount` is what `buildScene` consults; the arrays stay small.
 */
const AREAS_LAYOUT: ClusterLayout = {
  ids: ["hidden.py"],
  x: Float32Array.from([0]),
  y: Float32Array.from([0]),
  r: Float32Array.from([8]),
  kinds: ["file"],
  colors: ["#ffffff"],
  areaOf: ["A"],
  areas: [
    { id: "A", cx: 0, cy: 0, r: 50, color: "#111111", count: 6000, label: "A" },
    { id: "B", cx: 4000, cy: 0, r: 50, color: "#222222", count: 3000, label: "B" },
    { id: "C", cx: -4000, cy: 0, r: 50, color: "#333333", count: 3001, label: "C" },
  ],
  bundles: [],
  edges: [
    ...Array.from({ length: 60 }, (_, i): RankedEdge => ({
      src: "a",
      dst: "b",
      type: "imports",
      index: i,
      srcArea: "A",
      dstArea: "B",
    })),
    ...Array.from({ length: 40 }, (_, i): RankedEdge => ({
      src: "b",
      dst: "a",
      type: "imports",
      index: 60 + i,
      srcArea: "B",
      dstArea: "A",
    })),
  ],
  grid: { "0,0": [0] },
  depth: 1,
  nodeCount: 12_001,
};

vi.mock("@/lib/graph/useAreaLayout", () => ({
  useAreaLayout: () => ({
    layout: layoutBox.current,
    pending: false,
    mode: "inline" as const,
    deaths: 0,
    slow: false,
  }),
}));

const hitTest = vi.hoisted(() => vi.fn());

vi.mock("@/lib/graph/bigLayout", async (orig) => {
  const actual = await orig<typeof import("@/lib/graph/bigLayout")>();
  hitTest.mockImplementation(actual.hitTest);
  return { ...actual, hitTest };
});

import { CodeGraphCanvas } from "@/features/code/CodeGraphCanvas";

/** jsdom's PointerEvent drops clientX. A MouseEvent of the same type keeps it. */
function pointer(host: HTMLElement, type: "pointerdown" | "pointerup" | "pointermove", x: number, y: number) {
  act(() => {
    host.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 }),
    );
  });
}

const SMALL_LAYOUT: ClusterLayout = {
  ids: ["shown.py"],
  x: Float32Array.from([0]),
  y: Float32Array.from([0]),
  r: Float32Array.from([8]),
  kinds: ["file"],
  colors: ["#ffffff"],
  areaOf: ["A"],
  areas: [{ id: "A", cx: 0, cy: 0, r: 40, color: "#111111", count: 1, label: "A" }],
  bundles: [],
  edges: [],
  grid: { "0,0": [0] },
  depth: 1,
  nodeCount: 1,
};

function mount(layout: ClusterLayout, nodeId: string | string[], onSelect = vi.fn()) {
  layoutBox.current = layout;
  const ids = Array.isArray(nodeId) ? nodeId : [nodeId];
  render(
    <CodeGraphCanvas
      nodes={ids.map((id) => ({ id, path: id, kind: "file", name: id }))}
      edges={[]}
      enabledTypes={["imports", "calls", "owns", "tested_by", "references"]}
      isolate={false}
      selectedId={null}
      onSelect={onSelect}
      truncated={false}
      totalNodes={layout.nodeCount}
      frameId={null}
    />,
  );
  const host = screen.getByTestId("code-graph-canvas");
  host.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 900,
      bottom: 560,
      width: 900,
      height: 560,
      toJSON() {
        return {};
      },
    }) as DOMRect;
  return { host, onSelect };
}

/** Screen position of a point on the fitted 60/40 bundle. */
function bundleScreen(t: number): { x: number; y: number } {
  const circle = screen.getByTestId("hull-shape-A");
  const camX = Number(circle?.getAttribute("cx"));
  const camY = Number(circle?.getAttribute("cy"));
  const k = Number(circle?.getAttribute("r")) / 50;
  const [bundle] = bundlesFromEdges(AREAS_LAYOUT.edges, AREAS_LAYOUT.areas);
  const stroke = bundleStrokes(bundle, k)[0];
  const p = quadPoint(stroke, t);
  return { x: p.x * k + camX, y: p.y * k + camY };
}

describe("areas-only canvas", () => {
  it("does not select a file that is not drawn when the pointer goes up inside a hull", () => {
    const { host, onSelect } = mount(AREAS_LAYOUT, "hidden.py");
    expect(screen.getByTestId("lod-caption")).toHaveTextContent(
      "Large map: zoom into an area to see its files",
    );
    const circle = screen.getByTestId("hull-shape-A");
    const clientX = Number(circle?.getAttribute("cx"));
    const clientY = Number(circle?.getAttribute("cy"));
    expect(Number.isFinite(clientX)).toBe(true);
    hitTest.mockClear();
    pointer(host, "pointerdown", clientX, clientY);
    pointer(host, "pointerup", clientX, clientY);
    expect(hitTest).not.toHaveBeenCalled();
    // A click that never reaches the handler would also skip the id. The
    // handler still runs, and it clears the selection instead of naming the file.
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("selects a file that is drawn on a small map", () => {
    const { host, onSelect } = mount(SMALL_LAYOUT, "shown.py");
    const circle = screen.getByTestId("hull-shape-A");
    const clientX = Number(circle?.getAttribute("cx"));
    const clientY = Number(circle?.getAttribute("cy"));
    expect(Number.isFinite(clientX)).toBe(true);
    hitTest.mockClear();
    pointer(host, "pointerdown", clientX, clientY);
    pointer(host, "pointerup", clientX, clientY);
    expect(hitTest).toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledWith("shown.py");
  });

  it("shows both bundle counts when the pointer rests between the old 11 samples", () => {
    const { host } = mount(AREAS_LAYOUT, "hidden.py");
    const at = bundleScreen(0.05);
    pointer(host, "pointermove", at.x, at.y);
    expect(screen.getByTestId("canvas-tip")).toHaveTextContent("A→B 60 · B→A 40");
  });
});

/**
 * Fit is areas-only because C is far away. A and B sit close enough that
 * zooming until A fills 35% of the short side still leaves B on screen.
 * `side.py` is the file inside that neighbour; `open.py` is inside A.
 */
const ZOOMED_LAYOUT: ClusterLayout = {
  ids: ["open.py", "side.py"],
  x: Float32Array.from([0, 160]),
  y: Float32Array.from([0, 0]),
  r: Float32Array.from([8, 8]),
  kinds: ["file", "file"],
  colors: ["#ffffff", "#ffffff"],
  areaOf: ["A", "B"],
  areas: [
    { id: "A", cx: 0, cy: 0, r: 100, color: "#111111", count: 6000, label: "A" },
    { id: "B", cx: 160, cy: 0, r: 40, color: "#222222", count: 3000, label: "B" },
    { id: "C", cx: 8000, cy: 0, r: 50, color: "#333333", count: 3001, label: "C" },
  ],
  bundles: [],
  edges: [],
  grid: { "0,0": [0], "16,0": [1] },
  depth: 1,
  nodeCount: 12_001,
};

function hullCircle(id: string): { x: number; y: number; r: number } {
  const circle = screen.getByTestId(`hull-shape-${id}`);
  return {
    x: Number(circle?.getAttribute("cx")),
    y: Number(circle?.getAttribute("cy")),
    r: Number(circle?.getAttribute("r")),
  };
}

describe("a revealed area on a map above 10k nodes", () => {
  it("does not select a file in a neighbouring hull that is still on screen", () => {
    const { host, onSelect } = mount(ZOOMED_LAYOUT, ["open.py", "side.py"]);
    expect(screen.getByTestId("lod-caption")).toHaveTextContent(
      "Large map: zoom into an area to see its files",
    );

    const start = hullCircle("A");
    pointer(host, "pointerdown", start.x, start.y);
    pointer(host, "pointermove", 450, 280);
    pointer(host, "pointerup", 450, 280);

    let guard = 0;
    while (
      screen.getByTestId("lod-caption").textContent?.includes("Large map") &&
      guard < 8
    ) {
      fireEvent.wheel(host, { clientX: 450, clientY: 280, deltaY: -400 });
      guard += 1;
    }
    expect(guard).toBeGreaterThan(0);
    expect(screen.getByTestId("lod-caption").textContent ?? "").not.toContain("Large map");

    const opened = hullCircle("A");
    // 35% of the short side (560). This is the threshold `revealedArea` uses.
    expect(opened.r).toBeGreaterThanOrEqual(560 * 0.35);

    const neighbour = hullCircle("B");
    expect(neighbour.x).toBeGreaterThan(0);
    expect(neighbour.x).toBeLessThan(900);
    expect(neighbour.y).toBeGreaterThan(0);
    expect(neighbour.y).toBeLessThan(560);

    pointer(host, "pointermove", neighbour.x, neighbour.y);
    expect(screen.queryByTestId("canvas-tip")).toBeNull();

    hitTest.mockClear();
    onSelect.mockClear();
    pointer(host, "pointerdown", neighbour.x, neighbour.y);
    pointer(host, "pointerup", neighbour.x, neighbour.y);
    expect(hitTest).toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(null);
    expect(onSelect).not.toHaveBeenCalledWith("side.py");

    const mine = hullCircle("A");
    onSelect.mockClear();
    pointer(host, "pointerdown", mine.x, mine.y);
    pointer(host, "pointerup", mine.x, mine.y);
    expect(onSelect).toHaveBeenCalledWith("open.py");

    onSelect.mockClear();
    fireEvent.doubleClick(host, { clientX: neighbour.x, clientY: neighbour.y });
    expect(onSelect).not.toHaveBeenCalledWith("side.py");
    expect(screen.getByTestId("lod-caption")).toHaveTextContent(
      "Large map: zoom into an area to see its files",
    );
  });
});
