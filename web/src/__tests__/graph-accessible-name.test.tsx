import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeEdge, CodeMap, CodeNode } from "@/lib/types";

/**
 * What a screen reader actually announces (GRPH-524).
 *
 * `graph-node-count.test.ts` guards the same clause by reading the SOURCE — it asserts that
 * the region after `Code graph: ${ids.length} nodes` contains the text `counts.undescribed`.
 * That is the right shape for the claim it makes (both surfaces derive from one memo, which
 * is a wiring fact no single render can show), and it cannot see this:
 *
 * ```tsx
 * (false
 *   ? `, ${counts.undescribed} referenced but not yet described. `
 *   : `. `)
 * ```
 *
 * One word. The interpolation stays exactly where the assertion looks for it, inside a branch
 * that can no longer execute. Full suite: 366 passed, tsc clean. The screen-reader surface
 * silently stops announcing the undescribed count, and the sighted caption still reports it —
 * so the a11y-parity defect the guard exists to prevent is back, with the guard green.
 *
 * **A string-presence check at any scope cannot tell live code from dead code.** So this file
 * renders. What a screen reader announces IS the rendered string; for that half there is no
 * substitute for reading it off the element.
 */
const node = (path: string): CodeNode => ({
  id: path, path, kind: "file", name: path, lang: "py", summary: "", fresh: true,
});

const edge = (src: string, dst: string): CodeEdge => ({ src, dst, type: "imports" });

function codeMap(nodes: CodeNode[], edges: CodeEdge[]): CodeMap {
  return {
    nodes, edges, node_count: nodes.length, edge_count: edges.length, outbound: [],
  };
}

/** Two described files; `c.py` exists only as an edge endpoint, so undescribed === 1. */
const WITH_UNDESCRIBED = codeMap(
  [node("a.py"), node("b.py")],
  [edge("a.py", "b.py"), edge("a.py", "c.py")],
);

/** Every endpoint is described, so undescribed === 0 and the clause must not appear. */
const ALL_DESCRIBED = codeMap([node("a.py"), node("b.py")], [edge("a.py", "b.py")]);

const map = vi.hoisted(() => ({ current: null as CodeMap | null }));

vi.mock("@/features/ProjectContext", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useProjectCtx: () => ({ activeId: "prj_1", setActiveId: vi.fn() }),
}));

vi.mock("@/lib/queries", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  useCodeMap: () => ({ data: map.current, isLoading: false }),
  useCodeAnalysis: () => ({ data: null, isLoading: false }),
  useFleetPresence: () => ({ data: null, isLoading: false }),
}));

/** jsdom has no layout, so CodeChat's autoscroll finds no `scrollTo` on its node — the
 *  same stub `tracker.test.tsx` installs for the same reason. */
beforeEach(() => {
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
});

async function announce(m: CodeMap): Promise<string> {
  map.current = m;
  const { CodeGraphView } = await import("@/features/code/CodeGraphView");
  render(<CodeGraphView />);
  const svg = screen.getByRole("application");
  return svg.getAttribute("aria-label") ?? "";
}

describe("the graph's accessible name", () => {
  it("announces the undescribed count when there is one", async () => {
    const label = await announce(WITH_UNDESCRIBED);

    // The specific clause, not merely a non-empty label. The prefix is unconditional, so
    // asserting the name exists — or that it mentions nodes — passes under the mutation
    // above and this test would be decoration.
    expect(label).toContain("1 referenced but not yet described");
  });

  it("says nothing about undescribed nodes when there are none", async () => {
    // The other direction, and the one that makes the first test mean something. Without it
    // the clause could be emitted unconditionally — announcing "0 referenced but not yet
    // described" on a fully described graph — and the first assertion would still pass.
    const label = await announce(ALL_DESCRIBED);

    expect(label).toContain("Code graph:");
    expect(label).not.toContain("referenced but not yet described");
  });

  it("counts the same nodes the sighted caption counts", async () => {
    // Parity is the original claim (GRPH-479): the caption said 193 and the accessible name
    // said 228 for one picture. Both surfaces read the same memo now — asserted here on what
    // each one renders, so a divergence has to survive being read aloud side by side.
    const label = await announce(WITH_UNDESCRIBED);

    expect(label).toContain("Code graph: 3 nodes");
    expect(screen.getByText(/3 nodes · 2 edges/)).toBeInTheDocument();
    expect(screen.getByText(/1 referenced but not yet described/)).toBeInTheDocument();
  });
});
