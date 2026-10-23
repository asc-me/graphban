import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FOCUS_ZOOM, framePoint } from "@/features/code/canvas/camera";
import { CodeGraphCanvas, type CanvasControlApi } from "@/features/code/CodeGraphCanvas";
import { CodeGraphView } from "@/features/code/CodeGraphView";
import type { ClusterLayout } from "@/lib/graph/bigLayout";
import { useCanvasPins } from "@/lib/graph/useGraphPins";
import type { CodeMap, CodeNode } from "@/lib/types";

const layoutBox = vi.hoisted(() => ({
  current: null as ClusterLayout | null,
}));

const mapBox = vi.hoisted(() => ({
  current: null as CodeMap | null,
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

vi.mock("@/lib/graph/useGraphLayout", () => ({
  useGraphLayout: () => ({ pos: {}, pending: false, relayout: vi.fn() }),
}));

vi.mock("@/features/ProjectContext", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useProjectCtx: () => ({ activeId: "prj_1", setActiveId: vi.fn() }),
}));

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: mapBox.current, isLoading: false, isError: false, refetch: vi.fn() }),
  useCodeAnalysis: () => ({ data: null, isLoading: false }),
  useFleetPresence: () => ({ data: null, isLoading: false }),
}));

const LAYOUT: ClusterLayout = {
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

const MAP_LAYOUT: ClusterLayout = {
  ...LAYOUT,
  layout: "map",
  areas: [
    {
      id: "A",
      cx: 0,
      cy: 0,
      r: 40,
      color: "#111111",
      count: 1,
      label: "A",
      rect: { x: -80, y: -80, w: 160, h: 160 },
    },
  ],
};

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
}

function cameraOf() {
  const circle = screen.getByTestId("hull-shape-A");
  const cx = Number(circle?.getAttribute("cx"));
  const cy = Number(circle?.getAttribute("cy"));
  const k = Number(circle?.getAttribute("r")) / 40;
  return { cx, cy, k };
}

type CanvasProps = ComponentProps<typeof CodeGraphCanvas>;

function element(onSelect: CanvasProps["onSelect"], over: Partial<CanvasProps> = {}) {
  return (
    <CodeGraphCanvas
      nodes={[{ id: "shown.py", path: "shown.py", kind: "file", name: "shown.py" }]}
      edges={[]}
      enabledTypes={["imports", "calls"]}
      isolate={false}
      selectedId={null}
      onSelect={onSelect}
      truncated={false}
      totalNodes={1}
      frameId={null}
      layout="clusters"
      onLayout={() => {}}
      {...over}
    />
  );
}

function mount(over: Partial<CanvasProps> = {}) {
  layoutBox.current = over.layout === "map" ? MAP_LAYOUT : LAYOUT;
  const onSelect = vi.fn();
  const controlRef: { current: CanvasControlApi | null } = { current: null };
  const view = render(element(onSelect, { controlRef, ...over }));
  const host = screen.getByTestId("code-graph-canvas");
  box(host);
  return { ...view, host, onSelect, controlRef };
}

function drag(host: HTMLElement, fromX: number, fromY: number, dx: number) {
  pointer(host, "pointerdown", fromX, fromY);
  pointer(host, "pointermove", fromX + dx, fromY);
  pointer(host, "pointerup", fromX + dx, fromY);
}

function clickAt(host: HTMLElement, x: number, y: number) {
  pointer(host, "pointerdown", x, y);
  pointer(host, "pointerup", x, y);
}

beforeEach(() => {
  layoutBox.current = LAYOUT;
  mapBox.current = null;
});

describe("canvas drag-to-pin", () => {
  it("pins in the current layout only, and hit-testing follows the pin back", () => {
    const { host, onSelect, rerender } = mount();
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();
    const { cx, cy } = cameraOf();
    const cam = host.getAttribute("data-camera-x");
    drag(host, cx, cy, 400);
    expect(host.getAttribute("data-camera-x")).toBe(cam);
    expect(screen.getByRole("button", { name: "Unpin all" })).toBeInTheDocument();

    onSelect.mockClear();
    clickAt(host, cx + 400, cy);
    expect(onSelect).toHaveBeenLastCalledWith("shown.py");
    onSelect.mockClear();
    clickAt(host, cx, cy);
    expect(onSelect).toHaveBeenLastCalledWith(null);

    rerender(element(onSelect, { layout: "map" }));
    expect(host).toHaveAttribute("data-layout", "map");
    onSelect.mockClear();
    clickAt(host, cx + 400, cy);
    expect(onSelect).toHaveBeenLastCalledWith(null);
    onSelect.mockClear();
    clickAt(host, cx, cy);
    expect(onSelect).toHaveBeenLastCalledWith("shown.py");
    expect(screen.getByRole("button", { name: "Unpin all" })).toBeInTheDocument();

    rerender(element(onSelect, { layout: "clusters" }));
    expect(host).toHaveAttribute("data-layout", "clusters");
    onSelect.mockClear();
    clickAt(host, cx + 400, cy);
    expect(onSelect).toHaveBeenLastCalledWith("shown.py");
  });

  it("hides Unpin all once it has cleared every pin", () => {
    const { host, onSelect } = mount();
    const { cx, cy } = cameraOf();
    drag(host, cx, cy, 400);
    fireEvent.click(screen.getByRole("button", { name: "Unpin all" }));
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();
    onSelect.mockClear();
    clickAt(host, cx + 400, cy);
    expect(onSelect).toHaveBeenLastCalledWith(null);
    onSelect.mockClear();
    clickAt(host, cx, cy);
    expect(onSelect).toHaveBeenLastCalledWith("shown.py");
  });

  it("frames the packed cell when Find is still armed and a regroup drops the pin", () => {
    const previous = window.matchMedia;
    window.matchMedia = (query: string) =>
      ({
        matches: query.includes("reduce"),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
        onchange: null,
      }) as unknown as MediaQueryList;
    try {
      const { host, rerender, onSelect } = mount({ frameId: "shown.py" });
      const before = cameraOf();
      expect(before.k).toBeCloseTo(FOCUS_ZOOM, 5);
      drag(host, before.cx, before.cy, 400);
      expect(cameraOf().cx).toBeCloseTo(before.cx, 5);

      pointer(host, "pointerdown", 8, 8);
      pointer(host, "pointermove", 80, 8);
      pointer(host, "pointerup", 80, 8);
      const panned = cameraOf();
      expect(panned.cx).not.toBeCloseTo(before.cx, 0);

      layoutBox.current = { ...LAYOUT, areaOf: ["B"] };
      rerender(element(onSelect, { frameId: "shown.py" }));
      expect(screen.getByTestId("canvas-pin-toast")).toHaveTextContent(
        "1 pins cleared: areas regrouped",
      );

      const packed = framePoint(0, 0, FOCUS_ZOOM, 900, 560);
      const pinWorldX = 400 / before.k;
      const abandoned = framePoint(pinWorldX, 0, FOCUS_ZOOM, 900, 560);
      const after = cameraOf();
      expect(after.k).toBeCloseTo(packed.k, 5);
      expect(after.cx).toBeCloseTo(packed.x, 3);
      expect(after.cy).toBeCloseTo(packed.y, 3);
      expect(Math.abs(after.cx - abandoned.x)).toBeGreaterThan(100);
      expect(host).toHaveAttribute("data-frame", "node");
    } finally {
      window.matchMedia = previous;
    }
  });

  it("clears a pin whose area changed and shows the regroup toast", () => {
    const { host, rerender, onSelect } = mount();
    const { cx, cy } = cameraOf();
    drag(host, cx, cy, 400);
    layoutBox.current = { ...LAYOUT, areaOf: ["B"], depth: 2 };
    rerender(element(onSelect));
    expect(screen.getByTestId("canvas-pin-toast")).toHaveTextContent("1 pins cleared: areas regrouped");
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();
    onSelect.mockClear();
    clickAt(host, cx, cy);
    expect(onSelect).toHaveBeenLastCalledWith("shown.py");
  });

  it("does not pin a press that stays within 4px, or a pan that missed the node", () => {
    const { host, onSelect } = mount();
    const { cx, cy } = cameraOf();
    clickAt(host, cx, cy);
    expect(onSelect).toHaveBeenCalledWith("shown.py");
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();

    pointer(host, "pointerdown", 8, 8);
    pointer(host, "pointermove", 40, 8);
    pointer(host, "pointerup", 40, 8);
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();
  });

  it("fits a pin that sits outside the hull", () => {
    const { host, controlRef } = mount();
    const before = cameraOf();
    const worldX = 700 / before.k;
    expect(before.cx + 700).toBeGreaterThan(900);
    drag(host, before.cx, before.cy, 700);
    act(() => {
      controlRef.current?.fit();
    });
    const after = cameraOf();
    const sx = worldX * after.k + after.cx;
    expect(sx).toBeGreaterThan(0);
    expect(sx).toBeLessThan(900);
  });

  it("pans while tracing, and a click picks the path instead of a pin", () => {
    const onPathTarget = vi.fn();
    const onPathEmpty = vi.fn();
    const { host, onSelect } = mount({
      pathState: { picking: true, pending: false, slow: false, path: null },
      onPathTarget,
      onPathEmpty,
    });
    const { cx, cy } = cameraOf();
    clickAt(host, cx, cy);
    expect(onPathTarget).toHaveBeenCalledWith("shown.py");
    expect(onSelect).not.toHaveBeenCalled();
    expect(onPathEmpty).not.toHaveBeenCalled();

    const before = host.getAttribute("data-camera-x");
    drag(host, cx, cy, 80);
    expect(host.getAttribute("data-camera-x")).not.toBe(before);
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("selects a map tile on an empty click and does not pin", () => {
    const { host, onSelect } = mount({ layout: "map" });
    const tile = screen.getByTestId("hull-shape-A");
    const rx = Number(tile?.getAttribute("x"));
    const ry = Number(tile?.getAttribute("y"));
    const rw = Number(tile?.getAttribute("width"));
    const k = rw / 160;
    const camX = rx + 80 * k;
    const camY = ry + 80 * k;
    clickAt(host, 40 * k + camX, 40 * k + camY);
    expect(onSelect).toHaveBeenLastCalledWith(null);
    expect(screen.getByTestId("lod-caption")).toHaveTextContent("Area bundles");
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();
  });

  it("keeps a pin when the canvas unmounts and the view still holds the store", () => {
    const onSelect = vi.fn();
    function Session({ show }: { show: boolean }) {
      const pins = useCanvasPins();
      if (!show) return <div data-testid="canvas-unmounted" />;
      return element(onSelect, { canvasPins: pins });
    }
    const view = render(<Session show />);
    let host = screen.getByTestId("code-graph-canvas");
    box(host);
    const before = cameraOf();
    drag(host, before.cx, before.cy, 400);
    const worldX = 400 / before.k;

    view.rerender(<Session show={false} />);
    expect(screen.getByTestId("canvas-unmounted")).toBeInTheDocument();

    view.rerender(<Session show />);
    host = screen.getByTestId("code-graph-canvas");
    box(host);
    const after = cameraOf();
    onSelect.mockClear();
    clickAt(host, worldX * after.k + after.cx, after.cy);
    expect(onSelect).toHaveBeenLastCalledWith("shown.py");
    expect(screen.getByRole("button", { name: "Unpin all" })).toBeInTheDocument();
  });
});

describe("the view keeps canvas pins across Matrix", () => {
  it("still shows Unpin all after the canvas unmounts and comes back", () => {
    const nodes: CodeNode[] = Array.from({ length: 801 }, (_, i) => ({
      id: `mod/n${i}.py`,
      path: `mod/n${i}.py`,
      kind: "file",
      name: `n${i}.py`,
      lang: "py",
      summary: "",
      fresh: true,
    }));
    mapBox.current = {
      nodes,
      edges: [],
      node_count: nodes.length,
      edge_count: 0,
      outbound: [],
    };
    render(<CodeGraphView />);
    const host = screen.getByTestId("code-graph-canvas");
    box(host);
    const { cx, cy } = cameraOf();
    drag(host, cx, cy, 400);
    expect(screen.getByRole("button", { name: "Unpin all" })).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("layout-chip-matrix"));
    expect(screen.queryByTestId("code-graph-canvas")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("layout-chip-clusters"));
    expect(screen.getByTestId("code-graph-canvas")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unpin all" })).toBeInTheDocument();
  });
});
