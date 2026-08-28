import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeEdge, CodeMap, CodeNode } from "@/lib/types";

/**
 * Alt-click traces a path, and no other modifier does (GRPH-560, PRD-20 AC-19).
 *
 * AC-19 names **alt-click** as the gesture. It is implemented at `CodeGraphView.tsx` behind
 * `ev.altKey && selPath`, and nothing exercised it: no file under `src/__tests__` mentioned
 * `altKey` at all. Measured on the ticket — rebind it to a different modifier and the PRD's
 * gesture is simply gone, while **all 383 tests across 45 files still pass**.
 *
 * The live verification does not cover it either, and that is the part worth knowing. The
 * independent review of GRPH-481 walked the feature end to end in a real browser
 * **deliberately by keyboard**, to prove the keyboard route was equal. Which it is — and it
 * means the alt-click affordance has been exercised by nobody: not by CI, not by the browser
 * pass. The gesture the PRD names was unfalsifiable.
 *
 * **The second test is the load-bearing one.** A test that only asserts alt-click traces a
 * path passes just as well against a handler that ignores the modifier entirely and traces on
 * every click — which would break ordinary selection completely. Pinning the gesture means
 * pinning what the *same click without the modifier* does.
 */
const node = (path: string): CodeNode => ({
  id: path, path, kind: "file", name: path, lang: "py", summary: "", fresh: true,
});

const edge = (src: string, dst: string): CodeEdge => ({ src, dst, type: "imports" });

const MAP: CodeMap = {
  nodes: [node("a.py"), node("b.py"), node("c.py")],
  edges: [edge("a.py", "b.py"), edge("b.py", "c.py")],
  node_count: 3, edge_count: 2, outbound: [],
};

const map = vi.hoisted(() => ({ current: null as CodeMap | null }));

vi.mock("@/features/ProjectContext", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useProjectCtx: () => ({ activeId: "prj_1", setActiveId: vi.fn() }),
}));

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: map.current, isLoading: false }),
  // The trace query is keyed on (a, b) and only enabled once both ends exist. Returning a
  // path unconditionally is what lets the ASSERTION be about the gesture rather than about
  // the query: if the handler sets `pathTo`, the trace panel appears; if it does not, `walk`
  // stays null and nothing renders, whatever this returns.
  useCodeAnalysis: () => ({
    data: {
      path: {
        a: "a.py", b: "b.py", found: true, missing: [],
        hops: [{ src: "a.py", dst: "b.py", type: "imports", forward: true }],
      },
    },
    isLoading: false,
  }),
  useFleetPresence: () => ({ data: null, isLoading: false }),
}));

beforeEach(() => {
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
});

async function graph() {
  map.current = MAP;
  const { CodeGraphView } = await import("@/features/code/CodeGraphView");
  render(<CodeGraphView />);
}

/** A node by its accessible name, which begins with the kind label and the path. */
const nodeButton = (path: string) =>
  screen.getByRole("button", { name: new RegExp(`\\b${path.replace(".", "\\.")},`) });

/** The trace panel only exists while a path is being traced. */
const tracing = () => screen.queryByRole("button", { name: "Clear the path" }) !== null;

describe("AC-19's alt-click gesture", () => {
  it("traces a path from the selection when alt is held", async () => {
    await graph();
    fireEvent.click(nodeButton("a.py"));          // select the start
    expect(tracing()).toBe(false);

    fireEvent.click(nodeButton("b.py"), { altKey: true });

    expect(tracing()).toBe(true);
  });

  it("selects instead of tracing when the modifier is absent", async () => {
    // THE DISCRIMINATING HALF. Without it, a handler that ignored `altKey` and traced on
    // every click would pass the test above — while making it impossible to select anything.
    await graph();
    fireEvent.click(nodeButton("a.py"));

    fireEvent.click(nodeButton("b.py"));

    expect(tracing()).toBe(false);
    expect(nodeButton("b.py")).toHaveAttribute("aria-pressed", "true");
  });

  it("does not trace on a different modifier", async () => {
    // The acceptance the ticket states: rebinding to any other modifier must fail. Shift is
    // the specific one to check, because the same handler already gives shift a different
    // job — widening the reach — so a rebind to shift would collide rather than merely move.
    await graph();
    fireEvent.click(nodeButton("a.py"));

    fireEvent.click(nodeButton("b.py"), { shiftKey: true });

    expect(tracing()).toBe(false);
  });

  it("needs a selection to trace from", async () => {
    // `ev.altKey && selPath` — alt-click with nothing selected selects, it does not trace
    // from nowhere. Pinned because dropping `selPath` from the condition leaves the two tests
    // above green.
    await graph();

    fireEvent.click(nodeButton("b.py"), { altKey: true });

    expect(tracing()).toBe(false);
    expect(nodeButton("b.py")).toHaveAttribute("aria-pressed", "true");
  });
});
