import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { readGraphUrl, replaceGraphUrl, writeGraphSearch } from "@/features/code/useGraphUrlState";
import type { CodeEdge, CodeMap, CodeNode } from "@/lib/types";

/**
 * PRD-52 D10. The view tests are the call: a correct parser that CodeGraphView
 * never consults would leave every one of them red.
 */

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
  ];
  return { nodes, edges, node_count: nodes.length, edge_count: edges.length, outbound: [] };
}

const map = vi.hoisted(() => ({ current: null as CodeMap | null }));

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: map.current, isLoading: false, isError: false, refetch: vi.fn() }),
  useCodeAnalysis: () => ({ data: null, isLoading: false }),
  useFleetPresence: () => ({ data: null, isLoading: false }),
}));

function at(search: string) {
  const href = search ? `/code?${search}` : "/code";
  window.history.replaceState(null, "", href);
}

function params() {
  return new URLSearchParams(window.location.search);
}

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

beforeEach(() => {
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
  map.current = bigMap();
  at("");
});

afterEach(() => {
  cleanup();
  at("");
});

async function mount() {
  const { CodeGraphView } = await import("@/features/code/CodeGraphView");
  render(<CodeGraphView />);
}

describe("graph URL codec", () => {
  it("keeps foreign params and omits the camera, pins and trail", () => {
    const next = writeGraphSearch("?ref=keep&select=GRPH-1", {
      layout: "matrix",
      sel: "p00/f0.py",
      depth: 2,
      iso: true,
      cell: { row: "p00", col: "p02" },
    });
    const got = new URLSearchParams(next);
    expect(got.get("ref")).toBe("keep");
    expect(got.get("select")).toBe("GRPH-1");
    expect(got.get("layout")).toBe("matrix");
    expect(got.get("sel")).toBe("p00/f0.py");
    expect(got.get("depth")).toBe("2");
    expect(got.get("iso")).toBe("1");
    expect(got.get("cell")).toBe("p00~p02");
    expect([...got.keys()].filter((k) => !["ref", "select", "layout", "sel", "depth", "iso", "cell"].includes(k))).toEqual([]);

    at("ref=keep&layout=orbit");
    const push = vi.spyOn(window.history, "pushState");
    replaceGraphUrl({ layout: "map", sel: null, depth: 1, iso: false, cell: null });
    expect(push).not.toHaveBeenCalled();
    expect(params().get("ref")).toBe("keep");
    expect(params().get("layout")).toBe("map");
    push.mockRestore();
  });

  it("reads a link once and treats a bad cell as present", () => {
    expect(readGraphUrl("?layout=matrix&cell=A~B&depth=2&iso=1&sel=a.py")).toMatchObject({
      layout: "matrix",
      sel: "a.py",
      depth: 2,
      iso: true,
      cell: { row: "A", col: "B" },
      cellPresent: true,
    });
    expect(readGraphUrl("?layout=nope&depth=9&iso=2&cell=notapair").cellPresent).toBe(true);
    expect(readGraphUrl("?layout=nope&depth=9&iso=2&cell=notapair").cell).toBeNull();
    expect(readGraphUrl("?layout=nope&depth=9&iso=2&cell=notapair").layout).toBeNull();
    expect(readGraphUrl("?layout=nope&depth=9&iso=2&cell=notapair").depth).toBeNull();
  });
});

describe("shareable graph view", () => {
  it("opens Matrix on the linked cell without adding a history entry", async () => {
    at("layout=matrix&cell=p00~p02&ref=keep");
    const before = window.history.length;
    const push = vi.spyOn(window.history, "pushState");
    await mount();
    const cell = await screen.findByTestId("matrix-cell-p00-p02");
    expect(cell).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("area-matrix")).toBeInTheDocument();
    expect(within(screen.getByTestId("graph-aside")).getByTestId("cell-inspector")).toBeInTheDocument();
    expect(params().get("ref")).toBe("keep");
    expect(params().get("cell")).toBe("p00~p02");
    expect(params().get("layout")).toBe("matrix");
    expect(push).not.toHaveBeenCalled();
    expect(window.history.length).toBe(before);
    push.mockRestore();
  });

  it("selects a linked node and zooms to it", async () => {
    at("sel=p00/f0.py&depth=2&iso=1");
    await mount();
    const aside = screen.getByTestId("graph-aside");
    await waitFor(() => expect(within(aside).getByText("f0.py")).toBeInTheDocument());
    expect(within(aside).getByText(/2 hops/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Isolate" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-frame", "node"));
    expect(params().get("sel")).toBe("p00/f0.py");
    expect(params().get("depth")).toBe("2");
    expect(params().get("iso")).toBe("1");
    expect(screen.queryByText("Trail")).not.toBeInTheDocument();
  });

  it("fits when the link names no node", async () => {
    await mount();
    await waitFor(() => {
      const canvas = screen.getByTestId("code-graph-canvas");
      expect(canvas.getAttribute("data-depth")).toBeTruthy();
    });
    expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-frame", "fit");
    expect(params().get("sel")).toBeNull();
  });

  it("drops an unknown node and says so", async () => {
    at("sel=no/such.py");
    await mount();
    expect(await screen.findByTestId("graph-link-toast")).toHaveTextContent("Linked node not in this map");
    await waitFor(() => {
      expect(within(screen.getByTestId("graph-aside")).queryByText("such.py")).not.toBeInTheDocument();
      expect(params().get("sel")).toBeNull();
    });
    await waitFor(() => {
      const canvas = screen.getByTestId("code-graph-canvas");
      expect(canvas.getAttribute("data-depth")).toBeTruthy();
    });
    expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-frame", "fit");
  });

  it("drops an unknown cell and a cell that does not parse", async () => {
    at("layout=matrix&cell=nope~gone");
    await mount();
    expect(await screen.findByTestId("graph-link-toast")).toHaveTextContent("Linked node not in this map");
    expect(screen.getByTestId("area-matrix")).toBeInTheDocument();
    expect(screen.queryByTestId("cell-inspector")).not.toBeInTheDocument();
    await waitFor(() => expect(params().get("cell")).toBeNull());
    expect(params().get("layout")).toBe("matrix");

    cleanup();
    at("layout=matrix&cell=notapair");
    await mount();
    expect(await screen.findByTestId("graph-link-toast")).toHaveTextContent("Linked node not in this map");
    expect(screen.queryByTestId("cell-inspector")).not.toBeInTheDocument();
    await waitFor(() => expect(params().get("cell")).toBeNull());
  });

  it("does not follow a search string written after mount", async () => {
    at("sel=p00/f0.py");
    await mount();
    const aside = screen.getByTestId("graph-aside");
    await waitFor(() => expect(within(aside).getByText("f0.py")).toBeInTheDocument());
    window.history.replaceState(null, "", "/code?sel=p05/f5.py");
    await new Promise((r) => setTimeout(r, 30));
    expect(within(aside).getByText("f0.py")).toBeInTheDocument();
    expect(within(aside).queryByText("f5.py")).not.toBeInTheDocument();
  });

  it("writes layout, selection, depth and Isolate with replaceState", async () => {
    at("ref=keep");
    const user = userEvent.setup();
    const before = window.history.length;
    const push = vi.spyOn(window.history, "pushState");
    await mount();
    await waitFor(() => expect(screen.getByTestId("code-graph-canvas")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Find a node"), { target: { value: "p01/f0.py" } });
    await user.click(within(screen.getByRole("listbox", { name: "Matching nodes" })).getByRole("option"));
    await waitFor(() => expect(params().get("sel")).toBe("p01/f0.py"));

    fireEvent.change(screen.getByLabelText("Find a node"), { target: { value: "p03/f3.py" } });
    await user.click(within(screen.getByRole("listbox", { name: "Matching nodes" })).getByRole("option"));
    await waitFor(() => expect(params().get("sel")).toBe("p03/f3.py"));
    expect(screen.getByText("Trail")).toBeInTheDocument();
    expect(params().get("trail")).toBeNull();
    expect(params().has("pin")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Light 2 rings around the selection" }));
    await waitFor(() => expect(params().get("depth")).toBe("2"));

    await user.click(screen.getByRole("button", { name: "Isolate" }));
    await waitFor(() => expect(params().get("iso")).toBe("1"));

    await user.click(screen.getByTestId("layout-chip-map"));
    await waitFor(() => expect(params().get("layout")).toBe("map"));

    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    const keys = [...params().keys()];
    expect(keys.filter((k) => !["ref", "layout", "sel", "depth", "iso", "cell"].includes(k))).toEqual([]);
    expect(params().get("ref")).toBe("keep");
    expect(push).not.toHaveBeenCalled();
    expect(window.history.length).toBe(before);
    push.mockRestore();
  });

  it("a linked node is not already on the trail", async () => {
    at("sel=p00/f0.py");
    const user = userEvent.setup();
    await mount();
    await waitFor(() => expect(within(screen.getByTestId("graph-aside")).getByText("f0.py")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Find a node"), { target: { value: "p04/f4.py" } });
    await user.click(within(screen.getByRole("listbox", { name: "Matching nodes" })).getByRole("option"));
    await waitFor(() => expect(params().get("sel")).toBe("p04/f4.py"));
    // One user pick after the link. The link itself is not a trail entry, so
    // the breadcrumb (which needs two) stays hidden.
    expect(screen.queryByText("Trail")).not.toBeInTheDocument();
  });
});
