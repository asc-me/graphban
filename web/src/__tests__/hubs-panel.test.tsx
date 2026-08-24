import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HubsPanel } from "@/features/code/HubsPanel";
import type { CodeHub } from "@/lib/types";

/**
 * PRD-20 AC-18 (GRPH-480): *"Ranks by inbound degree. A file importing forty things must not
 * outrank one forty things import."*
 *
 * The fixture is the LIVE GraphBan graph, not invented numbers, because it already contains the
 * discriminating case and inventing one risks inventing an easier one. `mcp_server.py` is
 * inbound 5 / outbound 18 — the biggest importer on the map and correctly not a hub — while
 * `config.py` is inbound 9 / outbound 0. Undirected degree ranks them 23 and 9, i.e. exactly
 * backwards, which is the failure this panel exists to avoid.
 */
const HUBS: CodeHub[] = [
  { path: "backend/app/models/__init__.py", inbound: 20, outbound: 2, kind: "file", described: true },
  { path: "web/src/lib/queries.ts", inbound: 14, outbound: 2, kind: "file", described: true },
  { path: "backend/app/config.py", inbound: 9, outbound: 0, kind: "file", described: true },
  // items.py and memory.py TIE on inbound 7. Without a tie in the fixture the deterministic
  // ordering below can never fail — the "test that never enters the state it is about" shape.
  { path: "backend/app/services/items.py", inbound: 7, outbound: 7, kind: "file", described: true },
  { path: "backend/app/services/memory.py", inbound: 7, outbound: 5, kind: "file", described: true },
  { path: "backend/app/mcp_server.py", inbound: 5, outbound: 18, kind: "file", described: true },
  { path: "backend/app/services/artifacts.py", inbound: 4, outbound: 3, kind: "file", described: false },
];

const ALL = ["imports", "calls", "owns", "tested_by", "references"];

function paint(over: Partial<React.ComponentProps<typeof HubsPanel>> = {}) {
  const props = {
    hubs: HUBS,
    sort: "inbound" as const,
    onSort: vi.fn(),
    onPick: vi.fn(),
    onClose: vi.fn(),
    selected: null,
    loading: false,
    edgeTypes: ALL,
    allEdgeTypes: ALL,
    kindColour: () => "#7ca2ff",
    ...over,
  };
  return { ...render(<HubsPanel {...props} />), props };
}

/** Row order as rendered, by path. */
function order() {
  return screen.getAllByRole("button")
    .map((b) => b.getAttribute("aria-label") ?? "")
    .filter((l) => l.includes(","))
    .map((l) => l.split(",")[0]);
}

describe("hubs rank by inbound, not by degree", () => {
  it("puts the most depended-on first", () => {
    paint();
    expect(order()[0]).toBe("backend/app/models/__init__.py");
  });

  it("ranks the biggest importer BELOW a file nothing imports from — the whole AC", () => {
    paint();
    const rows = order();

    // config.py: 9 in / 0 out. mcp_server.py: 5 in / 18 out.
    // By undirected degree that is 9 vs 23 and this assertion flips.
    expect(rows.indexOf("backend/app/config.py"))
      .toBeLessThan(rows.indexOf("backend/app/mcp_server.py"));
  });

  it("shows BOTH directions on every row, because one number hides the distinction", () => {
    paint();
    const row = screen.getByRole("button", { name: /mcp_server\.py/ });

    expect(within(row).getByText("5")).toBeTruthy();
    expect(within(row).getByText("18")).toBeTruthy();
  });

  it("draws the two bars in proportion, on one shared scale", () => {
    const { container } = paint();
    const width = (name: RegExp, which: 0 | 1) => {
      const row = screen.getByRole("button", { name });
      const fill = row.querySelectorAll<HTMLElement>("span[style*='width']")[which];
      return parseFloat(fill.style.width);
    };

    // The first draft of this panel shipped with widths that did nothing at all — the fills
    // were inline elements, so every `width` was a no-op and all six rows drew empty tracks.
    // A chart whose bars do not encode the numbers is worse than a table.
    expect(width(/models\/__init__\.py/, 0)).toBeCloseTo(100, 0);
    expect(width(/config\.py/, 0)).toBeCloseTo(45, 0);
    expect(width(/models\/__init__\.py/, 0)).toBeGreaterThan(width(/config\.py/, 0));

    // Shared scale across rows, not per-row normalisation: config.py's 9 must read as smaller
    // than models' 20, which per-row scaling would draw identically.
    expect(container.querySelectorAll("span[style*='width']").length).toBeGreaterThan(6);
  });

  it("gives an outbound of zero no bar at all, rather than a hairline that reads as one", () => {
    paint();
    const row = screen.getByRole("button", { name: /config\.py/ });
    const out = row.querySelectorAll<HTMLElement>("span[style*='width']")[1];

    expect(parseFloat(out.style.width)).toBe(0);
  });

  it("names both directions in the accessible label", () => {
    paint();
    expect(
      screen.getByRole("button", {
        name: "backend/app/config.py, 9 depend on it, it depends on 0",
      }),
    ).toBeTruthy();
  });

  it("calls out the row whose rank looks wrong until you read both numbers", () => {
    paint();
    const inverted = screen.getByRole("button", { name: /mcp_server\.py/ });
    const ordinary = screen.getByRole("button", { name: /models\/__init__\.py/ });

    // The teaching moment: sixth place looks like a bug on the biggest importer on the map,
    // and the callout is what stops a reader "fixing" the ranking back to undirected degree.
    expect(inverted.textContent).toMatch(/imports the most here, and is not a hub/i);
    expect(ordinary.textContent).not.toMatch(/is not a hub/i);
  });

  it("does not flag the inverted row when sorted the other way", () => {
    paint({ sort: "outbound" });
    // Sorted by outbound it is first, so nothing about its rank needs explaining.
    expect(screen.getByRole("button", { name: /mcp_server\.py/ }).textContent)
      .not.toMatch(/is not a hub/i);
  });

  it("marks a node undescribed rather than letting it pass as described", () => {
    paint();
    expect(screen.getByRole("button", { name: /artifacts\.py.*not described/ })).toBeTruthy();
  });
});

describe("the other direction is a different question, and says so", () => {
  it("re-ranks and retitles when sorted by outbound", () => {
    paint({ sort: "outbound" });

    expect(order()[0]).toBe("backend/app/mcp_server.py");
    expect(screen.getByText("What this would drag with it.")).toBeTruthy();
  });

  it("names the two directions in plain language, never as 'degree'", () => {
    paint();
    const depended = screen.getByRole("button", { name: "Depended on" });
    const depends = screen.getByRole("button", { name: "Depends on" });

    // The CONTROL is what must not conflate them. "Undirected degree would rank it first"
    // appears in the inverted-row callout and is the opposite of the problem — it names the
    // wrong measure in order to warn about it, which is why this is scoped to the control.
    expect(depended.textContent).not.toMatch(/degree/i);
    expect(depends.textContent).not.toMatch(/degree/i);
    expect(screen.getByText("What breaks if this changes.")).toBeTruthy();
  });
});

describe("an empty list means no data, never 'no hubs'", () => {
  it("says nothing has been described when no filter is on", () => {
    paint({ hubs: [] });
    expect(screen.getByText(/nothing here has been described yet/i)).toBeTruthy();
  });

  it("never states the absence as a finding", () => {
    const { container } = paint({ hubs: [] });

    // The HEADLINE is the claim. "No hubs." asserts something about the codebase from a graph
    // that covers a minority of it; "No edges to rank." reports the state of the data. The
    // first survived a sabotage that only the small print was watching.
    expect(screen.getByText("No edges to rank.")).toBeTruthy();
    expect(container.textContent).not.toMatch(/no hubs/i);
  });

  it("blames the filter when one is on, rather than the graph", () => {
    paint({ hubs: [], edgeTypes: ["calls"] });
    expect(screen.getByText(/no edges of the types currently shown/i)).toBeTruthy();
  });

  it("does not claim to rank every edge type while scoped to some", () => {
    // The footnote is split across elements by its <b>, so match on the assembled text.
    const { container } = paint({ edgeTypes: ["calls", "owns"] });
    expect(container.textContent).toMatch(/Ranked by inbound calls \+ owns edges/);
  });

  it("claims no scope when every edge type is shown", () => {
    const { container } = paint();
    expect(container.textContent).toMatch(/Ranked by inbound edges/);
    expect(container.textContent).not.toMatch(/inbound imports \+ calls/);
  });
});

describe("the list does not reshuffle between identical reads", () => {
  it("breaks a tie on the path, not on input order", () => {
    paint();
    const rows = order();

    // Both are inbound 7. Ties must resolve the same way every render, or the panel appears
    // to move on its own while nothing has changed.
    expect(rows.indexOf("backend/app/services/items.py"))
      .toBeLessThan(rows.indexOf("backend/app/services/memory.py"));
  });

  it("gives the same order when the same hubs arrive in a different order", () => {
    paint();
    const first = order();
    cleanup();
    paint({ hubs: [...HUBS].reverse() });

    expect(order()).toEqual(first);
  });
});

describe("picking a hub", () => {
  it("hands the path back so the view can select and frame it", () => {
    const { props } = paint();
    fireEvent.click(screen.getByRole("button", { name: /config\.py/ }));
    expect(props.onPick).toHaveBeenCalledWith("backend/app/config.py");
  });

  it("shows which row is the selected node", () => {
    paint({ selected: "backend/app/config.py" });
    expect(screen.getByRole("button", { name: /config\.py/ }).getAttribute("aria-current")).toBe("true");
  });
});
