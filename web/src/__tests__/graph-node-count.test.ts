import { describe, expect, it } from "vitest";

/**
 * One picture, one node count (GRPH-479).
 *
 * The caption under the title read `map.node_count` — the SERVER's count of described nodes
 * — while the canvas drew `allIds`, which is that set plus every edge endpoint nobody has
 * described yet. Measured on the live graph: the caption said **193 nodes** and the SVG's
 * accessible name said **228 nodes**, with 228 focusable groups actually in the DOM. A
 * sighted reader and a screen-reader user were told different things about the same picture,
 * and the accessible name was the accurate one.
 *
 * Neither number was a bug on its own. The defect was that there were two of them, computed
 * four hundred lines apart from different sources, with nothing able to notice they had
 * drifted — the same shape as `safe_path` vs `_inside` in the fleet package, which is pinned
 * for the same reason.
 *
 * **Asserted against source, because the claim is about the wiring.** A render test proves
 * what one build happened to pass; this proves what the file says, which is what a future
 * edit changes. `graph-call-sites.test.ts` pins its call-site conventions the same way and
 * gives the argument in full.
 *
 * **WHAT THIS FILE CANNOT SEE, and where the other half lives.** A string-presence check at
 * any scope cannot tell live code from dead code. Changing `counts.undescribed ?` to `false ?`
 * leaves the interpolation exactly where the assertions below look for it, inside a branch
 * that never runs — every test here passes and the screen-reader surface stops announcing the
 * count (GRPH-524). What is announced is the RENDERED string, so that half is asserted off the
 * element in `graph-accessible-name.test.tsx`. The two are complements, not duplicates: this
 * file proves both surfaces read one memo, which no single render can show; that one proves
 * the branch is reachable, which no source read can show.
 */
const VIEWS = import.meta.glob("../features/code/CodeGraphView.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

function source(): string {
  const hit = Object.values(VIEWS)[0];
  expect(hit, "CodeGraphView.tsx must be readable or these assertions mean nothing").toBeTruthy();
  return hit;
}

describe("the graph reports one node count", () => {
  it("never renders the server's described-only count as the headline", () => {
    // `map.node_count` counts CodeNode rows. The canvas draws described nodes UNION every
    // edge endpoint, so using it as the caption undercounts by exactly the dangling ones.
    //
    // Matched on the INTERPOLATION rather than the bare name, because the docstring
    // explaining this defect necessarily mentions it — an assertion that forbids naming the
    // problem also forbids writing down why it was one.
    expect(source()).not.toContain("${map.node_count}");
    expect(source()).not.toContain("${map.edge_count}");
  });

  it("derives the caption and the accessible name from the same memo", () => {
    const src = source();
    expect(src).toContain("${counts.drawn} nodes · ${counts.edges} edges");

    // EACH SURFACE IS CHECKED SEPARATELY, and a sabotage is why. Counting occurrences of
    // `counts.undescribed` across the whole file cannot tell "both surfaces have it" from
    // "one does" — dropping it from either left the other's two mentions behind and the
    // assertion passed. A11y parity is a claim about two specific places, so it has to be
    // asserted in two specific places.
    const region = (anchor: string, span = 500) => {
      const at = src.indexOf(anchor);
      expect(at, `${anchor} must exist`).toBeGreaterThan(0);
      return src.slice(at, at + span);
    };

    expect(
      region("The codebase as agents described it"),
      "the visible caption dropped the undescribed count",
    ).toContain("counts.undescribed");

    expect(
      region("Code graph: ${ids.length} nodes"),
      "the accessible name dropped the undescribed count, so a screen-reader user is told less",
    ).toContain("counts.undescribed");
  });

  it("counts what is drawn, not what was described", () => {
    const src = source();
    const at = src.indexOf("const counts = React.useMemo");
    expect(at, "the single definition must exist").toBeGreaterThan(0);
    const memo = src.slice(at, at + 400);

    expect(memo).toContain("drawn: allIds.length");
    expect(memo).toContain("undescribed: Math.max(0, allIds.length - nodes.length)");
  });

  it("surfaces the undescribed nodes rather than folding them into a total", () => {
    // "35 of these are only names an edge pointed at" is the interesting half — it is the
    // outstanding describe_code work. A single total hides it, which is how it stayed
    // invisible long enough for two counts to disagree without anyone noticing.
    expect(source()).toContain("referenced but not yet described");
  });
});
