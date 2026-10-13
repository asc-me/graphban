import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeEdge, CodeMap, CodeNode, FleetPresence, HeldArea } from "@/lib/types";

/**
 * Where `Build.dc.html` puts the code graph's chrome (GRPH-1028).
 *
 * The design gives the canvas a top-left find overlay, one bottom row of legend-and-controls,
 * and a 300px right aside for everything that describes the selection. The app had find in the
 * place header, the controls in the top-right corner, and FOUR floating panels competing for
 * the canvas's bottom-left — the legend, the trail, the inspector and the stub card all
 * claimed it, so whichever rendered last covered the rest.
 *
 * These tests are about WHERE a control lives, so they mostly assert containment in the real
 * DOM: the defect was two panels with plausible classes landing on the same pixels. Each
 * `toContainElement` has its negative half for the same reason the isolate test keeps the
 * dimmed case — "it is in the aside" means nothing unless "it is not still on the canvas"
 * is checked too.
 *
 * One claim does NOT survive that treatment, and the sabotage pass is what showed it: giving
 * the control cluster back its `absolute right-3 top-3` root failed nothing, because the
 * element stays the bottom bar's child and jsdom has no layout to disagree with. So the last
 * test here reads the source instead — narrowly, for self-positioning on the roots of the
 * five panels a parent now lays out. That is the one property a render test in this
 * environment genuinely cannot hold.
 */
const GRAPH_SRC = Object.values(
  import.meta.glob("../features/code/CodeGraphView.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>,
)[0];
const node = (path: string, kind = "file"): CodeNode => ({
  id: path, path, kind, name: path, lang: "py", summary: "", fresh: true,
});

const edge = (src: string, dst: string): CodeEdge => ({ src, dst, type: "imports" });

const MAP: CodeMap = {
  // Two KINDS on purpose: the legend explains a colour vocabulary, and a vocabulary of one
  // word explains nothing.
  nodes: [node("a.py"), node("b.py", "symbol"), node("far.py")],
  edges: [edge("a.py", "b.py")],
  node_count: 3,
  edge_count: 1,
  outbound: [],
};

const HELD: HeldArea = {
  area: "a.py",
  item_id: "GRPH-1",
  expires_at: "2026-08-14T12:10:00.000Z",
  agent_id: "GRPH-A1",
  agent_label: "claude @ mbp",
  active_role: "worker",
  state: "working",
  user_id: "u1",
  user_initials: "AC",
  user_color: "#a78bfa",
  node_paths: ["a.py"],
  predicted: false,
};

/** A fleet is held so the legend renders — an idle fleet draws nothing, by design. */
const PRESENCE: FleetPresence = {
  served_at: "2026-08-14T12:00:00.000Z",
  heartbeat_interval_seconds: 50,
  held: [HELD],
  off_map: [],
  truncated: false,
  total: 1,
};

vi.mock("@/features/ProjectContext", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useProjectCtx: () => ({ activeId: "prj_1", setActiveId: vi.fn() }),
}));

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: MAP, isLoading: false }),
  useCodeAnalysis: () => ({ data: null, isLoading: false }),
  useFleetPresence: () => ({ data: PRESENCE, isLoading: false }),
}));

beforeEach(() => {
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
});

async function mount() {
  const { CodeGraphView } = await import("@/features/code/CodeGraphView");
  render(<CodeGraphView />);
  return userEvent.setup();
}

const aside = () => screen.getByTestId("graph-aside");
const bottomBar = () => screen.getByTestId("graph-bottom-bar");
/** The canvas is what the bottom bar is laid over — the region the design keeps for the graph. */
const canvas = () => bottomBar().parentElement!;

/** A node on the canvas. Keyed on the path, not the kind — the fixture has more than one. */
function canvasNode(path: string) {
  return screen.queryByRole("button", {
    name: new RegExp(`\\b${path.replace(/\./g, "\\.")},`),
  });
}

describe("find, as a canvas overlay", () => {
  it("sits on the canvas and not in the place header", async () => {
    // The negative half alone would also pass if find had been deleted, so the input is
    // located first and the header is then checked for it.
    await mount();

    const input = screen.getByRole("textbox", { name: "Find a node" });
    expect(canvas()).toContainElement(input);
    expect(screen.getByTestId("place-header")).not.toContainElement(input);
  });

  it("names the hits instead of counting them, and a hit is a way in", async () => {
    // THE criterion. The header box could only show a number: "1" tells you the search
    // worked and not which node, on a canvas where the hit may be off-screen. A list that
    // renders but does not navigate would be the same dead end, so the click is asserted.
    const user = await mount();

    await user.type(screen.getByRole("textbox", { name: "Find a node" }), "far");
    const hit = within(screen.getByRole("listbox", { name: "Matching nodes" })).getByRole(
      "option",
      { name: /far\.py/ },
    );

    await user.click(hit);

    expect(within(aside()).getByText("far.py")).toBeInTheDocument();
  });

  it("says a query matched nothing rather than drawing an empty box", async () => {
    const user = await mount();

    await user.type(screen.getByRole("textbox", { name: "Find a node" }), "zzzzz");

    expect(screen.getByText("No node matches that.")).toBeInTheDocument();
  });
});

describe("the design's bottom row", () => {
  it("carries the legend and the controls as one row, not two corners", async () => {
    // `left:16px;right:16px;bottom:14px;justify-content:space-between` — one bar with the
    // legend at one end and the cluster at the other. Both halves are asserted in the SAME
    // element, which is what a return to `absolute right-3 top-3` breaks.
    await mount();
    const bar = bottomBar();

    expect(bar).toContainElement(screen.getByRole("button", { name: "Fit" }));
    expect(bar).toContainElement(screen.getByRole("button", { name: "fleet" }));
    expect(bar).toContainElement(screen.getByRole("button", { name: "Isolate" }));
  });

  it("says what the node colours mean, for the kinds actually drawn", async () => {
    // Every node is the same circle; only colour separates a file from a symbol, and nothing
    // on the page said so. The "actually drawn" half is the real claim — a fixed five-row key
    // over a graph of nothing but files is a glossary, so `config` must be absent here.
    await mount();
    const bar = within(bottomBar());

    expect(bar.getByText("File")).toBeInTheDocument();
    expect(bar.getByText("Symbol")).toBeInTheDocument();
    expect(bar.queryByText("Config")).not.toBeInTheDocument();
    expect(bar.queryByText("Module")).not.toBeInTheDocument();
  });

  it("leaves the five moved panels with no positioning of their own", () => {
    // Each of these used to pin itself to a canvas corner, and three of them picked the SAME
    // corner. They are now laid out by the bottom bar or by the aside column, which only
    // holds while none of them re-asserts a corner — a root that positions itself is not
    // being laid out by its parent, whatever the DOM tree says.
    const offenders: string[] = [];
    for (const name of [
      "GraphControls",
      "NodeInspector",
      "TrailBreadcrumb",
      "InteractionHint",
      "StubEvidence",
    ]) {
      const m = GRAPH_SRC.match(
        new RegExp(`function ${name}\\([\\s\\S]*?<div className="([^"]*)"`),
      );
      expect(m, `${name} must be readable or this assertion means nothing`).toBeTruthy();
      if (/\b(absolute|fixed|sticky)\b/.test(m![1])) offenders.push(`${name}: ${m![1]}`);
    }
    expect(offenders).toEqual([]);
  });
});

describe("the design's 300px aside", () => {
  it("holds the selection inspector, so it stops covering the graph", async () => {
    const user = await mount();

    await user.click(canvasNode("a.py")!);

    const path = screen.getByRole("button", { name: /Path from here/ });
    expect(aside()).toContainElement(path);
    expect(canvas()).not.toContainElement(path);
  });

  it("holds the trail once there are two stops", async () => {
    const user = await mount();

    await user.click(canvasNode("a.py")!);
    await user.click(canvasNode("b.py")!);

    const clear = screen.getByRole("button", { name: "Clear trail" });
    expect(aside()).toContainElement(clear);
    expect(canvas()).not.toContainElement(clear);
  });

  it("explains the gestures while nothing is selected, and inspects once something is", async () => {
    // The design's "Explore" state. This was a dismissible card pinned to the canvas's
    // bottom-right — the corner the control cluster now needs — and the two cannot both
    // have it, which is why the hint moved rather than being drawn twice.
    const user = await mount();

    expect(within(aside()).getByText("Explore")).toBeInTheDocument();

    await user.click(canvasNode("a.py")!);

    expect(within(aside()).queryByText("Explore")).not.toBeInTheDocument();
    expect(within(aside()).getByRole("button", { name: /Path from here/ })).toBeInTheDocument();
  });

  it("keeps the chat, and brings Inspect back in front when a node is picked", async () => {
    // The design has no chat on this page, but its own empty-state copy says to "ask a
    // question on the right", so the surface is kept behind a tab. The risk that creates is
    // a selection landing in a pane nobody can see — a lit ring and no inspector — so the
    // pull-forward is the half worth testing.
    const user = await mount();

    await user.click(screen.getByRole("button", { name: "Ask the codebase" }));
    expect(within(aside()).getByText(/Ask me about the codebase/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Path from here/ })).not.toBeInTheDocument();

    await user.click(canvasNode("a.py")!);

    expect(screen.getByRole("button", { name: "Inspect" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(aside()).getByRole("button", { name: /Path from here/ })).toBeInTheDocument();
  });
});
