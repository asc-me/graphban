import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CodeGraphCanvas, type CanvasControlApi } from "@/features/code/CodeGraphCanvas";
import type { ClusterLayout } from "@/lib/graph/bigLayout";

const layoutBox = vi.hoisted(() => ({
  current: null as ClusterLayout | null,
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

function pointer(host: HTMLElement, type: "pointerdown" | "pointerup" | "pointermove", x: number, y: number) {
  act(() => {
    host.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 }),
    );
  });
}

function cameraOf() {
  const circle = screen.getByTestId("hull-A").querySelector("circle");
  const cx = Number(circle?.getAttribute("cx"));
  const cy = Number(circle?.getAttribute("cy"));
  const k = Number(circle?.getAttribute("r")) / 40;
  return { cx, cy, k };
}

function mount(layoutId = "clusters") {
  layoutBox.current = LAYOUT;
  const onSelect = vi.fn();
  const controlRef: { current: CanvasControlApi | null } = { current: null };
  const view = render(
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
      controlRef={controlRef}
      layoutId={layoutId}
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

describe("canvas drag-to-pin", () => {
  it("pins in the current layout only, and hit-testing follows the pin back", () => {
    const { host, onSelect, rerender } = mount();
    expect(screen.queryByRole("button", { name: "Unpin all" })).not.toBeInTheDocument();
    const { cx, cy } = cameraOf();
    drag(host, cx, cy, 400);
    expect(screen.getByRole("button", { name: "Unpin all" })).toBeInTheDocument();

    onSelect.mockClear();
    clickAt(host, cx + 400, cy);
    expect(onSelect).toHaveBeenLastCalledWith("shown.py");
    onSelect.mockClear();
    clickAt(host, cx, cy);
    expect(onSelect).toHaveBeenLastCalledWith(null);

    rerender(
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
        layoutId="map"
      />,
    );
    onSelect.mockClear();
    clickAt(host, cx + 400, cy);
    expect(onSelect).toHaveBeenLastCalledWith(null);
    onSelect.mockClear();
    clickAt(host, cx, cy);
    expect(onSelect).toHaveBeenLastCalledWith("shown.py");
    expect(screen.getByRole("button", { name: "Unpin all" })).toBeInTheDocument();

    rerender(
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
        layoutId="clusters"
      />,
    );
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

  it("clears a pin whose area changed and shows the regroup toast", () => {
    const { host, rerender, onSelect } = mount();
    const { cx, cy } = cameraOf();
    drag(host, cx, cy, 400);
    layoutBox.current = { ...LAYOUT, areaOf: ["B"], depth: 2 };
    rerender(
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
      />,
    );
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
});
