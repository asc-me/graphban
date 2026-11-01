import { act, render, screen, within } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FOCUS_ZOOM, framePoint } from "@/features/code/canvas/camera";
import type { ClusterLayout } from "@/lib/graph/bigLayout";
import {
  announceNode,
  degreeOrder,
  holderClause,
  LISTBOX_WINDOW,
  matchTypeahead,
  stepIndex,
  windowBounds,
} from "@/lib/graph/useGraphKeyboard";

/**
 * PRD-52 D6 keyboard half, on the canvas. The SVG hook keeps a roving tabindex.
 * This surface is a virtualized listbox: one tab stop, aria-activedescendant,
 * degree order, never the viewport.
 */
const layoutBox = vi.hoisted(() => ({ current: null as ClusterLayout | null }));

vi.mock("@/lib/graph/useAreaLayout", () => ({
  useAreaLayout: () => ({
    layout: layoutBox.current,
    pending: false,
    mode: "inline" as const,
    deaths: 0,
    slow: false,
  }),
}));

import { CodeGraphCanvas } from "@/features/code/CodeGraphCanvas";

function layoutFor(ids: string[], xs: number[]): ClusterLayout {
  return {
    ids,
    x: Float32Array.from(xs),
    y: Float32Array.from(ids.map(() => 0)),
    r: Float32Array.from(ids.map(() => 6)),
    kinds: ids.map(() => "file"),
    colors: ids.map(() => "#fff"),
    areaOf: ids.map(() => "A"),
    areas: [{ id: "A", cx: 0, cy: 0, r: 40, color: "#111", count: ids.length, label: "A" }],
    bundles: [],
    edges: [],
    grid: {},
    depth: 1,
    nodeCount: ids.length,
  };
}

function fileNodes(n: number) {
  return Array.from({ length: n }, (_, i) => {
    const name = `n${String(i).padStart(2, "0")}.py`;
    return { id: `p/${name}`, path: `p/${name}`, kind: "file", name };
  });
}

function press(key: string) {
  const list = screen.getByRole("listbox", { name: "Graph nodes" });
  act(() => {
    list.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
  });
}

function mount(opts: {
  count?: number;
  xs?: number[];
  edges?: { src: string; dst: string; type: string }[];
  enabledTypes?: string[];
  onSelect?: (id: string | null) => void;
  holderOf?: (id: string) => { known: boolean; label?: string | null };
  selectedId?: string | null;
  pathPending?: boolean;
  pathArmed?: boolean;
  onAbortPath?: () => void;
  onDisarmPath?: () => void;
  nodes?: { id: string; path: string; kind: string; name: string }[];
  grid?: Record<string, number[]>;
}) {
  const nodes = opts.nodes ?? fileNodes(opts.count ?? 3);
  const xs = opts.xs ?? nodes.map((_, i) => (i === 0 ? 0 : 80_000 * i));
  layoutBox.current = layoutFor(nodes.map((n) => n.id), xs);
  if (opts.grid) layoutBox.current = { ...layoutBox.current, grid: opts.grid };
  render(
    <CodeGraphCanvas
      nodes={nodes}
      edges={opts.edges ?? []}
      enabledTypes={opts.enabledTypes ?? ["imports", "calls", "owns", "tested_by", "references"]}
      isolate={false}
      selectedId={opts.selectedId ?? null}
      onSelect={opts.onSelect ?? (() => {})}
      truncated={false}
      totalNodes={nodes.length}
      frameId={null}
      holderOf={opts.holderOf}
      pathPending={opts.pathPending}
      pathArmed={opts.pathArmed}
      onAbortPath={opts.onAbortPath}
      onDisarmPath={opts.onDisarmPath}
    />,
  );
  return { nodes, host: screen.getByTestId("code-graph-canvas") };
}

describe("listbox order", () => {
  it("sorts by degree, then path, and windows about 30 rows", () => {
    const nodes = fileNodes(40);
    const order = degreeOrder(nodes.map((n, i) => ({ ...n, degree: i === 39 ? 9 : 0 })));
    expect(order[0].id).toBe("p/n39.py");
    expect(order[1].id).toBe("p/n00.py");
    const all = windowBounds(40, -1);
    expect(all.end - all.start).toBe(LISTBOX_WINDOW);
    const tail = windowBounds(40, 39);
    expect(tail.end - tail.start).toBe(LISTBOX_WINDOW);
    expect(tail.end).toBe(40);
    expect(stepIndex(40, 39, 1)).toBe(39);
    expect(stepIndex(40, -1, -1)).toBe(39);
  });

  it("type-ahead walks the whole order, not the mounted window", () => {
    const nodes = fileNodes(40);
    expect(matchTypeahead(nodes, -1, "n")).toBe(0);
    expect(matchTypeahead(nodes, 0, "n3")).toBe(30);
    expect(matchTypeahead(nodes, 30, "n39")).toBe(39);
  });

  it("names a holder only when someone looked", () => {
    const node = { kind: "file", path: "p/a.py", degree: 2 };
    expect(holderClause(undefined)).toBe("holder unknown");
    expect(holderClause({ known: false })).toBe("holder unknown");
    expect(holderClause({ known: true, label: null })).toBe("not held");
    expect(announceNode(node, { known: true, label: "AD" })).toBe(
      "File p/a.py, 2 connections, held by AD",
    );
  });
});

describe("canvas listbox", () => {
  const frames: FrameRequestCallback[] = [];

  beforeEach(() => {
    frames.length = 0;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      frames.push(cb);
      return frames.length;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});
    vi.stubGlobal("matchMedia", () => ({
      matches: false,
      addEventListener() {},
      removeEventListener() {},
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("is one tab stop, and arrows, Home, End and type-ahead reach every node", () => {
    const { host } = mount({ count: 40 });
    const list = screen.getByRole("listbox", { name: "Graph nodes" });

    expect(list.getAttribute("tabindex")).toBe("0");
    expect(host.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    expect(within(list).getAllByRole("option")).toHaveLength(LISTBOX_WINDOW);
    for (const opt of within(list).getAllByRole("option")) {
      expect(opt.hasAttribute("tabindex")).toBe(false);
      expect(opt.getAttribute("aria-setsize")).toBe("40");
    }
    // The last node is outside the mounted window and off the fitted camera.
    expect(within(list).queryByText(/n39\.py/)).toBeNull();

    press("Home");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("File p/n00.py, 0 connections, holder unknown");

    press("End");
    const active = document.getElementById(list.getAttribute("aria-activedescendant") ?? "");
    expect(active).toHaveTextContent("p/n39.py");
    expect(active).toHaveAttribute("aria-posinset", "40");
    expect(active).toHaveAttribute("aria-setsize", "40");
    expect(within(list).getAllByRole("option")).toHaveLength(LISTBOX_WINDOW);

    press("ArrowDown");
    expect(list.getAttribute("aria-activedescendant")).toBe(active?.id);

    press("ArrowUp");
    expect(document.getElementById(list.getAttribute("aria-activedescendant") ?? "")).toHaveAttribute(
      "aria-posinset",
      "39",
    );
  });

  it("puts the highest degree first, on screen or not", () => {
    const nodes = fileNodes(8);
    const hub = nodes[7].id;
    mount({
      nodes,
      edges: nodes.slice(0, 5).map((n) => ({ src: hub, dst: n.id, type: "imports" })),
      xs: nodes.map((_, i) => (i === 7 ? 80_000 : 0)),
    });
    press("Home");
    expect(screen.getByTestId("graph-live")).toHaveTextContent(`File ${hub}, 5 connections`);
    expect(screen.getByRole("listbox", { name: "Graph nodes" }).querySelector('[aria-posinset="1"]')).toHaveTextContent(
      hub,
    );
  });

  it("type-ahead reaches a node that was not mounted", () => {
    mount({ count: 40 });
    const list = screen.getByRole("listbox", { name: "Graph nodes" });
    expect(within(list).queryByText(/n39\.py/)).toBeNull();
    press("n");
    press("3");
    press("9");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("p/n39.py");
    expect(within(list).getByText(/n39\.py/)).toBeInTheDocument();
    expect(within(list).getAllByRole("option").length).toBeLessThanOrEqual(LISTBOX_WINDOW);
  });

  it("Enter selects the active node and does not select on arrows", () => {
    const onSelect = vi.fn();
    mount({ count: 4, onSelect });
    press("ArrowDown");
    press("ArrowDown");
    expect(onSelect).not.toHaveBeenCalled();
    press("Enter");
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("p/n01.py");
  });

  it("moves through off-screen nodes without waiting for the camera frame", () => {
    const { host } = mount({ count: 5 });
    const ran = frames.length;
    act(() => {
      const list = screen.getByRole("listbox", { name: "Graph nodes" });
      for (let i = 0; i < 5; i++) {
        list.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
      }
    });
    // Five presses: the first node is in the fitted view, the other four are not.
    // The count is written in the key handler. Running the scheduled frames must not be required.
    expect(host.dataset.followCount).toBe("4");
    expect(host.dataset.follow).toBe("p/n04.py");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("p/n04.py");
    expect(frames.length).toBeGreaterThanOrEqual(ran);
    expect(frames.every((cb) => typeof cb === "function")).toBe(true);
    expect(host.getAttribute("data-tween")).toBe("1");
    expect(host.getAttribute("data-camera-k")).not.toBe("7.000");
  });

  it("cancels the tween when the next key lands on a node already in frame", () => {
    const { host } = mount({ count: 5 });
    act(() => {
      const list = screen.getByRole("listbox", { name: "Graph nodes" });
      for (const key of ["ArrowDown", "ArrowDown", "ArrowUp"]) {
        list.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
      }
    });
    // n00 is inside the fitted camera. Down, down aims a tween at n01. Up returns
    // to n00 in the same turn, so the flight must stop instead of finishing toward n01.
    expect(screen.getByTestId("graph-live")).toHaveTextContent("p/n00.py");
    expect(host.dataset.follow).toBe("in-view");
    expect(host.getAttribute("data-tween")).toBe("0");

    press("ArrowDown");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("p/n01.py");
    expect(host.dataset.follow).toBe("p/n01.py");
    expect(host.getAttribute("data-tween")).toBe("1");
  });

  it("frames a listbox follow of a pinned node at the pin, not the packed cell", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: q.includes("reduce"),
      addEventListener() {},
      removeEventListener() {},
    }));
    const packedX = 80_000;
    const { host } = mount({
      count: 2,
      xs: [0, packedX],
      // hitTest only walks the spatial grid. 80000 / GRID_CELL (10) is cell 8000.
      grid: { "0,0": [0], "8000,0": [1] },
    });
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

    press("End");
    const packed = framePoint(packedX, 0, FOCUS_ZOOM, 900, 560);
    expect(Number(host.getAttribute("data-camera-x"))).toBeCloseTo(packed.x, 3);

    const dragPx = 700;
    const cx = 900 / 2;
    const cy = 560 / 2;
    act(() => {
      for (const [type, x] of [
        ["pointerdown", cx],
        ["pointermove", cx + dragPx],
        ["pointerup", cx + dragPx],
      ] as const) {
        host.dispatchEvent(
          new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: cy, button: 0 }),
        );
      }
    });
    expect(screen.getByRole("button", { name: "Unpin all" })).toBeInTheDocument();

    press("End");
    const pinWorldX = (cx + dragPx - packed.x) / FOCUS_ZOOM;
    const pinned = framePoint(pinWorldX, 0, FOCUS_ZOOM, 900, 560);
    const cameraX = Number(host.getAttribute("data-camera-x"));
    expect(cameraX).toBeCloseTo(pinned.x, 3);
    expect(Math.abs(cameraX - packed.x)).toBeGreaterThan(100);
    expect(host.dataset.follow).toBe("p/n01.py");
  });

  it("jumps the camera when reduced motion is on", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: q.includes("reduce"),
      addEventListener() {},
      removeEventListener() {},
    }));
    const { host } = mount({ count: 3 });
    press("End");
    expect(host.dataset.follow).toBe("p/n02.py");
    expect(host.getAttribute("data-tween")).toBe("0");
    expect(host.getAttribute("data-camera-k")).toBe("7.000");
  });

  it("announces kind, path, connections and whatever the holder read actually was", () => {
    mount({
      count: 2,
      edges: [{ src: "p/n00.py", dst: "p/n01.py", type: "imports" }],
      holderOf: (id) => (id.endsWith("n00.py") ? { known: true, label: "AD" } : { known: true, label: null }),
    });
    press("Home");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("File p/n00.py, 1 connections, held by AD");
    press("ArrowDown");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("File p/n01.py, 1 connections, not held");
  });

  it("says the holder is unknown when presence was not passed", () => {
    mount({ count: 1, xs: [0] });
    press("Home");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("holder unknown");
  });

  it("puts node and edge counts on the canvas", () => {
    mount({
      count: 3,
      xs: [0, 0, 0],
      edges: [
        { src: "p/n00.py", dst: "p/n01.py", type: "imports" },
        { src: "p/n00.py", dst: "p/n02.py", type: "calls" },
      ],
      enabledTypes: ["imports"],
    });
    const label = screen.getByRole("application").getAttribute("aria-label") ?? "";
    expect(label).toContain("3 nodes, 1 of 2 edges");
  });

  it("Escape aborts a path request, then disarms, then clears the selection", () => {
    function Harness() {
      const [pending, setPending] = React.useState(true);
      const [armed, setArmed] = React.useState(false);
      const [selected, setSelected] = React.useState<string | null>("p/n00.py");
      return (
        <>
          <span data-testid="pending">{String(pending)}</span>
          <span data-testid="armed">{String(armed)}</span>
          <span data-testid="selected">{selected ?? ""}</span>
          <CodeGraphCanvas
            nodes={fileNodes(2)}
            edges={[]}
            enabledTypes={["imports"]}
            isolate={false}
            selectedId={selected}
            onSelect={setSelected}
            truncated={false}
            totalNodes={2}
            frameId={null}
            pathPending={pending}
            pathArmed={armed}
            onAbortPath={() => {
              setPending(false);
              setArmed(true);
            }}
            onDisarmPath={() => setArmed(false)}
          />
        </>
      );
    }
    layoutBox.current = layoutFor(["p/n00.py", "p/n01.py"], [0, 0]);
    render(<Harness />);
    press("Escape");
    expect(screen.getByTestId("pending")).toHaveTextContent("false");
    expect(screen.getByTestId("armed")).toHaveTextContent("true");
    expect(screen.getByTestId("selected")).toHaveTextContent("p/n00.py");
    press("Escape");
    expect(screen.getByTestId("armed")).toHaveTextContent("false");
    expect(screen.getByTestId("selected")).toHaveTextContent("p/n00.py");
    press("Escape");
    expect(screen.getByTestId("selected")).toHaveTextContent("");
  });

  it("drops a type-ahead buffer after the gap", () => {
    const now = vi.spyOn(Date, "now");
    now.mockReturnValue(5_000);
    const nodes = [
      ...fileNodes(2),
      { id: "p/zebra.py", path: "p/zebra.py", kind: "file", name: "zebra.py" },
    ];
    mount({ nodes, xs: [0, 0, 0] });
    press("n");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("n00.py");
    now.mockReturnValue(5_000 + 600);
    press("z");
    expect(screen.getByTestId("graph-live")).toHaveTextContent("zebra.py");
  });
});
