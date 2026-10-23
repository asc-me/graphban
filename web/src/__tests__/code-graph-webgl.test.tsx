import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ClusterLayout } from "@/lib/graph/bigLayout";
import { areaMatrix } from "@/features/code/canvas/reduced";

const LAYOUT: ClusterLayout = {
  ids: ["a.py"],
  x: Float32Array.from([10]),
  y: Float32Array.from([12]),
  r: Float32Array.from([8]),
  kinds: ["file"],
  colors: ["#7ca2ff"],
  areaOf: ["A"],
  areas: [
    { id: "A", cx: 0, cy: 0, r: 40, color: "#111111", count: 1, label: "A" },
    { id: "B", cx: 80, cy: 0, r: 30, color: "#222222", count: 1, label: "B" },
  ],
  bundles: [],
  edges: [
    { src: "a", dst: "b", type: "imports", index: 0, srcArea: "A", dstArea: "B" },
    { src: "a", dst: "b", type: "imports", index: 1, srcArea: "A", dstArea: "B" },
  ],
  grid: { "1,1": [0] },
  depth: 1,
  nodeCount: 1,
};

vi.mock("@/lib/graph/useAreaLayout", () => ({
  useAreaLayout: () => ({
    layout: LAYOUT,
    pending: false,
    mode: "inline" as const,
    deaths: 0,
    slow: false,
  }),
}));

import { CodeGraphCanvas } from "@/features/code/CodeGraphCanvas";

const original = HTMLCanvasElement.prototype.getContext;

function noop2d(): CanvasRenderingContext2D {
  return new Proxy({} as CanvasRenderingContext2D, {
    get(target, prop) {
      if (prop in target) return target[prop as keyof CanvasRenderingContext2D];
      return () => {};
    },
    set(target, prop, value) {
      (target as unknown as Record<string, unknown>)[prop as string] = value;
      return true;
    },
  });
}

function fakeGL(): WebGL2RenderingContext {
  const gl = {
    VERTEX_SHADER: 0x8b31,
    FRAGMENT_SHADER: 0x8b30,
    COMPILE_STATUS: 0x8b81,
    LINK_STATUS: 0x8b82,
    ARRAY_BUFFER: 0x8892,
    STATIC_DRAW: 0x88e4,
    DYNAMIC_DRAW: 0x88e8,
    FLOAT: 0x1406,
    TRIANGLES: 4,
    BLEND: 0x0be2,
    SRC_ALPHA: 0x0302,
    ONE_MINUS_SRC_ALPHA: 0x0303,
    COLOR_BUFFER_BIT: 0x4000,
    DEPTH_TEST: 0x0b71,
    createShader: () => ({}),
    shaderSource() {},
    compileShader() {},
    getShaderParameter: () => true,
    getShaderInfoLog: () => "",
    deleteShader() {},
    createProgram: () => ({}),
    attachShader() {},
    linkProgram() {},
    getProgramParameter: () => true,
    getProgramInfoLog: () => "",
    deleteProgram() {},
    getUniformLocation: () => ({}),
    createBuffer: () => ({}),
    deleteBuffer() {},
    createVertexArray: () => ({}),
    deleteVertexArray() {},
    bindBuffer() {},
    bindVertexArray() {},
    bufferData() {},
    enableVertexAttribArray() {},
    vertexAttribPointer() {},
    vertexAttribDivisor() {},
    enable() {},
    disable() {},
    blendFunc() {},
    useProgram() {},
    uniform1f() {},
    uniform2f() {},
    uniform3f() {},
    uniform4f() {},
    viewport() {},
    clearColor() {},
    clear() {},
    drawArraysInstanced() {},
    isContextLost: () => false,
  };
  return gl as unknown as WebGL2RenderingContext;
}

afterEach(() => {
  HTMLCanvasElement.prototype.getContext = original;
});

function mount() {
  render(
    <CodeGraphCanvas
      nodes={[{ id: "a.py", path: "a.py", kind: "file", name: "a.py" }]}
      edges={[]}
      enabledTypes={["imports", "calls", "owns", "tested_by", "references"]}
      isolate={false}
      selectedId={null}
      onSelect={() => {}}
      truncated={false}
      totalNodes={1}
      frameId={null}
    />,
  );
}

describe("renderer selection", () => {
  it("uses WebGL2 when the context exists", async () => {
    const gl = fakeGL();
    HTMLCanvasElement.prototype.getContext = function (type: string) {
      if (type === "webgl2") return gl;
      if (type === "2d") return noop2d();
      return null;
    } as typeof original;
    mount();
    await waitFor(() => expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-renderer", "webgl"));
  });

  it("uses Canvas 2D when WebGL2 is unavailable", async () => {
    mount();
    await waitFor(() => expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-renderer", "canvas2d"));
    expect(screen.queryByTestId("reduced-view")).not.toBeInTheDocument();
  });

  it("falls back to Matrix with a reduced view notice when neither context exists", async () => {
    HTMLCanvasElement.prototype.getContext = (() => null) as typeof original;
    mount();
    expect(await screen.findByTestId("reduced-view")).toHaveTextContent(/reduced view/i);
    expect(screen.getByTestId("reduced-matrix")).toBeInTheDocument();
    expect(screen.getByTestId("reduced-matrix")).toHaveTextContent("2");
  });

  it("switches to Canvas 2D on context loss without leaving a blank bitmap", async () => {
    const gl = fakeGL();
    HTMLCanvasElement.prototype.getContext = function (type: string) {
      if (type === "webgl2") return gl;
      if (type === "2d") return noop2d();
      return null;
    } as typeof original;
    mount();
    await waitFor(() => expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-renderer", "webgl"));
    const glCanvas = screen.getByTestId("code-graph-webgl");
    const d2 = screen.getByTestId("code-graph-2d") as HTMLCanvasElement;
    act(() => {
      glCanvas.dispatchEvent(new Event("webglcontextlost"));
    });
    expect(d2.width).toBeGreaterThan(0);
    expect(d2.hidden).toBe(false);
    expect((glCanvas as HTMLCanvasElement).hidden).toBe(true);
    expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-renderer", "canvas2d");
  });
});

describe("reduced matrix", () => {
  it("counts directed area pairs", () => {
    const matrix = areaMatrix(LAYOUT.areas, LAYOUT.edges);
    expect(matrix.counts[0][1]).toBe(2);
    expect(matrix.counts[1][0]).toBe(0);
  });
});
