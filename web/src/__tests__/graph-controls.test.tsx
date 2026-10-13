import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeEdge, CodeMap, CodeNode } from "@/lib/types";

/**
 * The canvas control cluster from `Build.dc.html` (GRPH-1026).
 *
 * The design's graph toolbar carries depth chips, an Isolate toggle, and a zoom group of
 * − / readout / + / Fit. Three of those had no control in the app at all: the view could only
 * be zoomed with the wheel and only reset by double-clicking the background. Both of those are
 * undiscoverable, and neither is reachable from a keyboard.
 *
 * These tests RENDER. A source-presence check cannot tell a live control from a dead one — the
 * lesson `graph-accessible-name.test.tsx` writes up at length — and the claim here is about
 * what a user can reach and what the canvas then shows.
 */
const node = (path: string): CodeNode => ({
  id: path, path, kind: "file", name: path, lang: "py", summary: "", fresh: true,
});

const edge = (src: string, dst: string): CodeEdge => ({ src, dst, type: "imports" });

/**
 * `a.py → b.py`, with `far.py` joined to neither. One ring from `a.py` therefore lights
 * a.py and b.py and leaves far.py outside it — which is what isolate has to act on.
 */
const MAP: CodeMap = {
  nodes: [node("a.py"), node("b.py"), node("far.py")],
  edges: [edge("a.py", "b.py")],
  node_count: 3,
  edge_count: 1,
  outbound: [],
};

vi.mock("@/features/ProjectContext", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useProjectCtx: () => ({ activeId: "prj_1", setActiveId: vi.fn() }),
}));

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: MAP, isLoading: false }),
  useCodeAnalysis: () => ({ data: null, isLoading: false }),
  useFleetPresence: () => ({ data: null, isLoading: false }),
}));

beforeEach(() => {
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
});

async function mount() {
  const { CodeGraphView } = await import("@/features/code/CodeGraphView");
  render(<CodeGraphView />);
  return userEvent.setup();
}

/** The zoom readout, as a number. The design renders it as a percentage. */
function zoomPct(): number {
  const el = screen.getByText(/^\d+%$/);
  return Number(el.textContent!.replace("%", ""));
}

/** A node on the canvas, addressed the way a screen reader would. */
function canvasNode(path: string) {
  return screen.queryByRole("button", { name: new RegExp(`File ${path},`) });
}

describe("the graph's zoom group", () => {
  it("renders −, a readout, + and Fit", async () => {
    await mount();

    expect(screen.getByRole("button", { name: "Zoom out" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Fit" })).toBeInTheDocument();
    expect(zoomPct()).toBe(100);
  });

  it("zooms in and out, and the readout follows the view", async () => {
    // THE criterion. A pair of buttons that render but move nothing is the whole defect this
    // replaces, so the assertion is on the readout changing in the right DIRECTION — not
    // merely that it changed, which a single hardcoded value would also satisfy.
    const user = await mount();

    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    const zoomedIn = zoomPct();
    expect(zoomedIn).toBeGreaterThan(100);

    await user.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(zoomPct()).toBeLessThan(zoomedIn);
  });

  it("Fit returns the view to 100%", async () => {
    const user = await mount();

    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(zoomPct()).not.toBe(100);

    await user.click(screen.getByRole("button", { name: "Fit" }));
    expect(zoomPct()).toBe(100);
  });

  it("stops at the zoom ceiling rather than running past it", async () => {
    // The control must not promise a zoom the viewport will clamp away: a button that stays
    // enabled while doing nothing reads as a broken control, which is the same class of
    // defect as the missing button it replaces.
    const user = await mount();
    const zin = screen.getByRole("button", { name: "Zoom in" });

    for (let i = 0; i < 12 && !(zin as HTMLButtonElement).disabled; i++) {
      await user.click(zin);
    }

    expect(zin).toBeDisabled();
    const ceiling = zoomPct();
    expect(screen.getByRole("button", { name: "Zoom out" })).toBeEnabled();
    expect(ceiling).toBe(600); // MAX_ZOOM
  });
});

describe("the graph's Isolate toggle", () => {
  it("is disabled until something is selected", async () => {
    // Isolate needs a reach set to isolate against. Enabled-but-inert would be the
    // absence-reads-as-clean shape: a control that looks available and does nothing.
    await mount();

    expect(screen.getByRole("button", { name: "Isolate" })).toBeDisabled();
  });

  it("HIDES what dimming would merely fade, and the control is the difference", async () => {
    // THE criterion, with its own control. `far.py` is outside the one-ring reach of `a.py`.
    // Selected but not isolated it must still be in the DOM (dimmed to 0.22) — that half is
    // what makes the second half mean something, because a test that only checked for absence
    // after isolate would also pass if the node had never rendered at all.
    const user = await mount();

    await user.click(canvasNode("a.py")!);
    expect(canvasNode("far.py")).toBeInTheDocument();

    const iso = screen.getByRole("button", { name: "Isolate" });
    expect(iso).toBeEnabled();
    await user.click(iso);

    expect(canvasNode("far.py")).not.toBeInTheDocument();
    // The reach itself survives: isolate hides the outside, it does not empty the canvas.
    expect(canvasNode("a.py")).toBeInTheDocument();
    expect(canvasNode("b.py")).toBeInTheDocument();
  });

  it("stands down when the selection clears, so the button never lies", async () => {
    // A pressed Isolate with nothing selected hides nothing while claiming to. Cleared here
    // through the inspector's close button because that is the only dismissal reachable with
    // focus on the toolbar: `kb.onKeyDown` is bound to the <svg>, so Escape does nothing once
    // focus has left the canvas. The stand-down is an effect on the reach set, so it covers
    // every clear path rather than just this one.
    const user = await mount();

    await user.click(canvasNode("a.py")!);
    await user.click(screen.getByRole("button", { name: "Isolate" }));
    expect(screen.getByRole("button", { name: "Isolate" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Close the inspector" }));

    const iso = screen.getByRole("button", { name: "Isolate" });
    expect(iso).toHaveAttribute("aria-pressed", "false");
    expect(iso).toBeDisabled();
    expect(canvasNode("far.py")).toBeInTheDocument();
  });
});

describe("the graph's depth chips", () => {
  it("offer the design's three rings and mark the live one", async () => {
    await mount();

    const chips = screen.getAllByRole("button", { name: /^Light \d ring/ });
    expect(chips.map((c) => c.textContent)).toEqual(["1", "2", "3"]);
    expect(chips[0]).toHaveAttribute("aria-pressed", "true");
    expect(chips[1]).toHaveAttribute("aria-pressed", "false");
  });

  it("moves the pressed chip when another ring is picked", async () => {
    // The chips replace a single `expand +1` button, so the state they advertise has to be
    // the state the canvas uses — pressed must follow the pick, not stay on 1.
    const user = await mount();

    await user.click(screen.getByRole("button", { name: /^Light 3 rings/ }));

    const chips = screen.getAllByRole("button", { name: /^Light \d ring/ });
    expect(chips[0]).toHaveAttribute("aria-pressed", "false");
    expect(chips[2]).toHaveAttribute("aria-pressed", "true");
  });
});
