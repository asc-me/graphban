import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PathTrace } from "@/features/code/PathTrace";
import type { CodePath } from "@/lib/types";

/**
 * PRD-20 AC-19 (GRPH-481): *"shortest path; hops report which way each edge actually points;
 * off-path dimmed."*
 *
 * The fixture is the route the live graph actually returns between `mcp_server.py` and
 * `queries.ts`, and it is useful precisely because it is mixed: hop one is walked AGAINST the
 * arrow and hop two with it. A same-direction fixture would let a trace that ignores direction
 * pass every assertion here.
 */
const FOUND: CodePath = {
  a: "backend/app/mcp_server.py",
  b: "web/src/lib/queries.ts",
  found: true,
  missing: [],
  hops: [
    { src: "backend/app/mcp_server.py", dst: "AGENTS.md", type: "references", forward: false },
    { src: "AGENTS.md", dst: "web/src/lib/queries.ts", type: "references", forward: true },
  ],
};

const ALL = ["imports", "calls", "owns", "tested_by", "references"];

function paint(path: CodePath, over: Partial<React.ComponentProps<typeof PathTrace>> = {}) {
  const props = {
    path,
    onClear: vi.fn(),
    onPick: vi.fn(),
    edgeTypes: ALL,
    allEdgeTypes: ALL,
    ...over,
  };
  return { ...render(<PathTrace {...props} />), props };
}

describe("a hop reports which way its edge actually points", () => {
  it("distinguishes an edge walked with the arrow from one walked against it", () => {
    const { container } = paint(FOUND);

    // The route is undirected; the REPORT is not. Both readings must be present, or the trace
    // answers "are these related" while looking like it answered "what depends on what".
    expect(screen.getByText("points back")).toBeTruthy();
    expect(screen.getByText("points this way")).toBeTruthy();
    expect(container.textContent).toMatch(/references/);
  });

  it("does not describe every hop the same way", () => {
    paint(FOUND);
    // A trace that dropped `forward` would render two identical rows and still look correct.
    expect(screen.queryAllByText("points this way")).toHaveLength(1);
    expect(screen.queryAllByText("points back")).toHaveLength(1);
  });

  it("shows the direction as a glyph too, not by colour alone", () => {
    const { container } = paint(FOUND);
    const arrows = [...container.querySelectorAll("[aria-hidden]")].map((e) => e.textContent);

    // Colour is a redundant channel and must stay redundant: someone who cannot tell accent
    // from purple still has the glyph, and someone using a screen reader has the words. This
    // asserts the glyph, because that is the one carrying the meaning on its own.
    expect(arrows).toContain("↓");
    expect(arrows).toContain("↑");
  });

  it("counts the hops, not the nodes", () => {
    paint(FOUND);
    expect(screen.getByText("2 hops")).toBeTruthy();
  });

  it("says hop, singular, for a direct edge", () => {
    paint({ ...FOUND, hops: [FOUND.hops[0]] });
    expect(screen.getByText("1 hop")).toBeTruthy();
  });

  it("lists both endpoints and every node between them", () => {
    const { container } = paint(FOUND);
    ["mcp_server.py", "AGENTS.md", "queries.ts"].forEach((n) =>
      expect(container.textContent).toContain(n),
    );
  });
});

describe("an unreachable pair and an undescribed one are different answers", () => {
  it("calls a missing endpoint a coverage gap, not an architectural boundary", () => {
    paint({ ...FOUND, found: false, hops: [], missing: ["backend/app/nope.py"] });

    expect(screen.getByText("Not on the map.")).toBeTruthy();
    expect(screen.getByText(/nothing has described it yet/i)).toBeTruthy();
    // The graph covers a minority of the tree, so "no path" here would report an absent
    // description as a fact about the architecture.
    expect(screen.queryByText(/no route between them/i)).toBeNull();
  });

  it("calls two known-but-unconnected nodes exactly that", () => {
    paint({ ...FOUND, found: false, hops: [], missing: [] });

    expect(screen.getByText("No route between them.")).toBeTruthy();
    expect(screen.getByText(/different components/i)).toBeTruthy();
    expect(screen.queryByText("Not on the map.")).toBeNull();
  });

  it("blames the edge filter when one is on, rather than the graph", () => {
    paint({ ...FOUND, found: false, hops: [], missing: [] }, { edgeTypes: ["calls"] });

    expect(screen.getByText(/nothing connects them through calls edges/i)).toBeTruthy();
    expect(screen.queryByText(/different components/i)).toBeNull();
  });

  it("does not claim a hop count when there is no route", () => {
    const { container } = paint({ ...FOUND, found: false, hops: [], missing: [] });
    expect(container.textContent).not.toMatch(/\d+ hops?/);
  });
});

describe("the footnote states the scope it actually walked", () => {
  it("names the edge types when scoped", () => {
    const { container } = paint(FOUND, { edgeTypes: ["calls", "owns"] });
    expect(container.textContent).toMatch(/over calls \+ owns edges/);
  });

  it("claims nothing extra when every type is shown", () => {
    const { container } = paint(FOUND);
    expect(container.textContent).toMatch(/each hop reports which way its edge points\./);
    expect(container.textContent).not.toMatch(/over .* edges/);
  });
});

describe("the trace is a way back into the graph", () => {
  it("hands a node back when one is picked", () => {
    const { props } = paint(FOUND);
    fireEvent.click(screen.getByText("AGENTS.md"));
    expect(props.onPick).toHaveBeenCalledWith("AGENTS.md");
  });

  it("clears on demand", () => {
    const { props } = paint(FOUND);
    fireEvent.click(screen.getByLabelText("Clear the path"));
    expect(props.onClear).toHaveBeenCalled();
  });

  it("renders the same trace identically twice", () => {
    const { container } = paint(FOUND);
    const first = container.textContent;
    cleanup();
    const { container: second } = paint(FOUND);
    expect(second.textContent).toBe(first);
  });
});
