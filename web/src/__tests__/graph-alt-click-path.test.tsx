import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CodeGraphCanvas } from "@/features/code/CodeGraphCanvas";
import { useCanvasPath } from "@/features/code/canvas/overlay";
import type { ClusterLayout } from "@/lib/graph/bigLayout";

/**
 * Alt-click traces a path, and no other modifier does (GRPH-560, PRD-20 AC-19).
 *
 * AC-19 names **alt-click** as the gesture. When every map moved to the canvas, the SVG
 * handler that carried it stopped being reachable, and the canvas had no alt-click at all:
 * the PRD's gesture was simply gone. These tests pin it on the canvas.
 *
 * **The plain-click test is the load-bearing one.** A test that only asserts alt-click
 * traces passes just as well against a handler that ignores the modifier and traces on
 * every click — which would break ordinary selection completely.
 */

const codeAnalysis = vi.hoisted(() => vi.fn());
const layoutBox = vi.hoisted(() => ({ current: null as ClusterLayout | null }));

vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, codeAnalysis: (...a: unknown[]) => codeAnalysis(...a) } };
});

vi.mock("@/lib/graph/useAreaLayout", () => ({
  useAreaLayout: () => ({ layout: layoutBox.current, pending: false, mode: "inline" as const, deaths: 0, slow: false }),
}));

vi.mock("@/lib/graph/bigLayout", async (orig) => ({
  ...(await orig<typeof import("@/lib/graph/bigLayout")>()),
  hitTest: () => "b.py",
}));

const LAYOUT: ClusterLayout = {
  ids: ["a.py", "b.py"],
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

function click(host: HTMLElement, mods: { altKey?: boolean; shiftKey?: boolean } = {}) {
  for (const type of ["pointerdown", "pointerup"]) {
    act(() => {
      host.dispatchEvent(
        new MouseEvent(type, { bubbles: true, cancelable: true, clientX: 450, clientY: 280, button: 0, ...mods }),
      );
    });
  }
}

function mount(selectedId: string | null) {
  cleanup();
  const onSelect = vi.fn();
  const onTraceTo = vi.fn();
  render(
    <CodeGraphCanvas
      nodes={[
        { id: "a.py", path: "a.py", kind: "file", name: "a.py" },
        { id: "b.py", path: "b.py", kind: "file", name: "b.py" },
      ]}
      edges={[{ src: "a.py", dst: "b.py", type: "imports" }]}
      enabledTypes={["imports"]}
      isolate={false}
      selectedId={selectedId}
      onSelect={onSelect}
      truncated={false}
      totalNodes={2}
      frameId={null}
      onTraceTo={onTraceTo}
    />,
  );
  const host = screen.getByTestId("code-graph-canvas");
  host.getBoundingClientRect = () =>
    ({ x: 0, y: 0, left: 0, top: 0, right: 900, bottom: 560, width: 900, height: 560, toJSON() { return {}; } }) as DOMRect;
  return { host, onSelect, onTraceTo };
}

beforeEach(() => {
  layoutBox.current = LAYOUT;
  codeAnalysis.mockReset();
  codeAnalysis.mockImplementation(() => new Promise(() => {}));
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
});

describe("AC-19's alt-click gesture", () => {
  it("traces a path from the selection when alt is held", () => {
    const { host, onSelect, onTraceTo } = mount("a.py");
    click(host, { altKey: true });
    expect(onTraceTo).toHaveBeenCalledWith("b.py");
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("selects instead of tracing when the modifier is absent", () => {
    const { host, onSelect, onTraceTo } = mount("a.py");
    click(host);
    expect(onSelect).toHaveBeenCalledWith("b.py");
    expect(onTraceTo).not.toHaveBeenCalled();
  });

  it("does not trace on a different modifier", () => {
    const { host, onSelect, onTraceTo } = mount("a.py");
    click(host, { shiftKey: true });
    expect(onTraceTo).not.toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledWith("b.py");
  });

  it("needs a selection to trace from", () => {
    const { host, onSelect, onTraceTo } = mount(null);
    click(host, { altKey: true });
    expect(onTraceTo).not.toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledWith("b.py");
  });

  it("runs the path read without arming path mode first", () => {
    const { result } = renderHook(() =>
      useCanvasPath({
        projectId: "prj_1",
        source: "a.py",
        edgeTypes: ["imports"],
        allEdgeTypes: ["imports"],
        onUseAllTypes: vi.fn(),
      }),
    );
    expect(result.current.picking).toBe(false);
    act(() => result.current.traceTo("b.py"));
    expect(codeAnalysis).toHaveBeenCalledTimes(1);
    expect(result.current.pending).toBe(true);
  });

  it("does not trace from a node to itself", () => {
    const { result } = renderHook(() =>
      useCanvasPath({
        projectId: "prj_1",
        source: "a.py",
        edgeTypes: ["imports"],
        allEdgeTypes: ["imports"],
        onUseAllTypes: vi.fn(),
      }),
    );
    act(() => result.current.traceTo("a.py"));
    expect(codeAnalysis).not.toHaveBeenCalled();
  });
});
