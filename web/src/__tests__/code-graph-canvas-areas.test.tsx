import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { bundlesFromEdges, bundleStrokes, quadPoint } from "@/features/code/canvas/edges";
import type { ClusterLayout, RankedEdge } from "@/lib/graph/bigLayout";

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
    layout: AREAS_LAYOUT,
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

function mount(onSelect = vi.fn()) {
  render(
    <CodeGraphCanvas
      nodes={[{ id: "hidden.py", path: "hidden.py", kind: "file", name: "hidden.py" }]}
      edges={[]}
      enabledTypes={["imports", "calls", "owns", "tested_by", "references"]}
      isolate={false}
      selectedId={null}
      onSelect={onSelect}
      truncated={false}
      totalNodes={12001}
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
  const circle = screen.getByTestId("hull-A").querySelector("circle");
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
    const { host, onSelect } = mount();
    expect(screen.getByTestId("lod-caption")).toHaveTextContent(
      "Large map: zoom into an area to see its files",
    );
    const circle = screen.getByTestId("hull-A").querySelector("circle");
    const clientX = Number(circle?.getAttribute("cx"));
    const clientY = Number(circle?.getAttribute("cy"));
    expect(Number.isFinite(clientX)).toBe(true);
    hitTest.mockClear();
    pointer(host, "pointerdown", clientX, clientY);
    pointer(host, "pointerup", clientX, clientY);
    expect(hitTest).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalledWith("hidden.py");
  });

  it("shows both bundle counts when the pointer rests between the old 11 samples", () => {
    const { host } = mount();
    const at = bundleScreen(0.05);
    pointer(host, "pointermove", at.x, at.y);
    expect(screen.getByTestId("canvas-tip")).toHaveTextContent("A→B 60 · B→A 40");
  });
});
