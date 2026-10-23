import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CANVAS_MAX_K } from "@/features/code/canvas/camera";
import { writeAlpha } from "@/features/code/canvas/scene";
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

describe("the graph's zoom group", () => {
  it("renders −, a readout, + and Fit", async () => {
    await mount();

    expect(screen.getByRole("button", { name: "Zoom out" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Fit" })).toBeInTheDocument();
    expect(zoomPct()).toBeGreaterThan(0);
  });

  it("zooms in and out, and the readout follows the view", async () => {
    // THE criterion. A pair of buttons that render but move nothing is the whole defect this
    // replaces, so the assertion is on the readout changing in the right DIRECTION — not
    // merely that it changed, which a single hardcoded value would also satisfy.
    const user = await mount();
    const fitted = zoomPct();

    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    const zoomedIn = zoomPct();
    expect(zoomedIn).toBeGreaterThan(fitted);

    await user.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(zoomPct()).toBeLessThan(zoomedIn);
  });

  it("Fit frames the map again, not a fixed 100%", async () => {
    // The SVG view's Fit reset to 100%, and on a real map 100% framed nothing: every node
    // sat outside the frame. The canvas opens fitted, so Fit has to land back there.
    const user = await mount();
    const fitted = zoomPct();

    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(zoomPct()).not.toBe(fitted);

    await user.click(screen.getByRole("button", { name: "Fit" }));
    expect(zoomPct()).toBe(fitted);
  });

  it("stops at the zoom ceiling rather than running past it", async () => {
    // The control must not promise a zoom the camera will clamp away: a button that stays
    // enabled while doing nothing reads as a broken control, which is the same class of
    // defect as the missing button it replaces.
    const user = await mount();
    const zin = screen.getByRole("button", { name: "Zoom in" });

    for (let i = 0; i < 40 && !(zin as HTMLButtonElement).disabled; i++) {
      await user.click(zin);
    }

    expect(zin).toBeDisabled();
    expect(screen.getByRole("button", { name: "Zoom out" })).toBeEnabled();
    expect(zoomPct()).toBe(CANVAS_MAX_K * 100);
  });
});

/** Select through Find, the way a user reaches a node the canvas draws but the DOM does not. */
async function select(user: ReturnType<typeof userEvent.setup>, path: string) {
  await user.type(screen.getByRole("textbox", { name: "Find a node" }), path);
  await user.click(
    within(screen.getByRole("listbox", { name: "Matching nodes" })).getByRole("option", {
      name: new RegExp(path.replace(".", "\\.")),
    }),
  );
}

describe("the graph's Isolate toggle", () => {
  it("is disabled until something is selected", async () => {
    // Isolate needs a reach set to isolate against. Enabled-but-inert would be the
    // absence-reads-as-clean shape: a control that looks available and does nothing.
    await mount();

    expect(screen.getByRole("button", { name: "Isolate" })).toBeDisabled();
  });

  it("fades what is outside the reach further than dimming does", () => {
    // PRD-52 D5: outside the reach dims to 0.22, or 0.06 under Isolate. The pair is the
    // claim — a buffer that wrote 0.06 either way would pass a check on the isolated half.
    const reach = Uint8Array.from([1, 1, 0]);
    const dimmed = writeAlpha(3, reach, false);
    const isolated = writeAlpha(3, reach, true);

    expect(dimmed[2]).toBe(Math.round(0.22 * 255));
    expect(isolated[2]).toBe(Math.round(0.06 * 255));
    expect(isolated[0]).toBe(255);
    expect(isolated[1]).toBe(255);
  });

  it("stands down when the selection clears, so the button never lies", async () => {
    // A pressed Isolate with nothing selected hides nothing while claiming to. The stand-down
    // is an effect on the reach set, so it covers every clear path rather than just this one.
    const user = await mount();

    await select(user, "a.py");
    await user.click(screen.getByRole("button", { name: "Isolate" }));
    expect(screen.getByRole("button", { name: "Isolate" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Close the inspector" }));

    const iso = screen.getByRole("button", { name: "Isolate" });
    expect(iso).toHaveAttribute("aria-pressed", "false");
    expect(iso).toBeDisabled();
  });
});

describe("the graph's depth chips", () => {
  it("offer the design's three rings and mark the live one", async () => {
    await mount();

    const chips = screen.getAllByRole("button", { name: /^Light \d ring/ });
    expect(chips.map((c) => c.textContent)).toEqual(["1 hop", "2 hops", "3 hops"]);
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
