import { describe, expect, it } from "vitest";

/**
 * PRD-20 D1 AC-3, D2's find claim, and the wiring both depend on.
 *
 * These three properties are **call-site conventions**, not behaviours of the hooks, and
 * that is why they kept being asserted in places where they could not fail:
 *
 * - AC-3, "positions survive edge filtering", cannot be a property of `layoutKey`. The hook
 *   memoizes on ids AND edges — the right call, since keying on ids alone would miss an
 *   edge a describe pass added — so the key MUST move when the edges do. What makes AC-3
 *   true is that the views never hand it the filtered set.
 * - D2's "find searches every node, including those inside collapsed components" is one
 *   argument: `useGraphFind(allIds, …)`. Passing the laid-out `ids` instead would search
 *   only what is on screen, which is exactly the search that cannot see what the view is
 *   hiding — and every existing find test would stay green, because `matchIds` is fine.
 *
 * Asserted against source because the claim IS about the wiring. A render test proves what
 * one build happened to pass; this proves what the file says, which is what a future edit
 * changes. `hierarchy.test.tsx` pins the org base the same way and for the same reason.
 */
const VIEWS = import.meta.glob("../features/{code/CodeGraphView,links/LinksGraphView}.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

function sourceOf(fragment: string): string {
  const hit = Object.entries(VIEWS).find(([path]) => path.includes(fragment));
  expect(hit, `${fragment} must be readable or these assertions mean nothing`).toBeTruthy();
  return hit![1];
}

/** The argument list of the first `call(` in `src`, flattened. */
function argsOf(src: string, call: string): string {
  const at = src.indexOf(call);
  expect(at, `${call} must exist`).toBeGreaterThan(0);
  const open = at + call.length - 1;
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "(") depth++;
    else if (src[i] === ")") {
      depth--;
      if (depth === 0) return src.slice(open + 1, i).replace(/\s+/g, " ");
    }
  }
  throw new Error(`unbalanced ${call}`);
}

describe("AC-3 lives at the call site, so it is pinned there", () => {
  it("CodeGraphView lays out over the UNFILTERED edges", () => {
    const src = sourceOf("CodeGraphView");
    const args = argsOf(src, "useGraphLayout(");

    // `edges` is the drawn set — `allEdges.filter(e => enabled[e.type])`. Handing it to the
    // layout is precisely the toggle-jank AC-3 exists to prevent.
    expect(args).toContain("layoutEdges");
    expect(args.split(",")[1].trim()).toBe("layoutEdges");
  });

  it("the layout edge set does not depend on which chips are enabled", () => {
    const src = sourceOf("CodeGraphView");
    const at = src.indexOf("const layoutEdges = React.useMemo(");
    expect(at).toBeGreaterThan(0);
    const memo = src.slice(at, src.indexOf("]);", at));
    // The dependency array is the guarantee: if `enabled` appeared here, a chip toggle
    // would recompute the layout input and move every node.
    expect(memo).not.toContain("enabled");
  });

  it("LinksGraphView lays out over its own unfiltered set", () => {
    const args = argsOf(sourceOf("LinksGraphView"), "useGraphLayout(");
    expect(args).toContain("layoutEdges");
  });
});

describe("find searches every node, not the visible subset", () => {
  it("CodeGraphView passes allIds, not the laid-out ids", () => {
    const args = argsOf(sourceOf("CodeGraphView"), "useGraphFind(");
    // `ids` is the entered-component subset in galaxy mode; searching it would make a match
    // inside a collapsed component unfindable — the one case D9 makes common.
    expect(args.split(",")[0].trim()).toBe("allIds");
  });
});
