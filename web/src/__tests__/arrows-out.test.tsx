import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ArrowsOut } from "@/features/code/CodeGraphView";
import type { ProjectStub } from "@/lib/types";

/**
 * PRD-21 D4.
 *
 * The anchoring is the point. Because D3 refused any edge that could not name the file
 * proving it, this arrow starts at the real node for `web/package.json` rather than
 * floating somewhere plausible — every arrow out is explainable by opening one file.
 */
const stub = (over: Partial<ProjectStub> = {}): ProjectStub => ({
  edge_id: "pe_1", project_id: "prj_core", tag: "CORE", name: "Core", accent: "#c6f24e",
  kind: "depends_on", resolved_name: "@acme/core", fresh: true,
  evidence: [{ file: "web/package.json", fact: "@acme/core ^2.1" }],
  anchor_paths: ["web/package.json"], unanchored: false, ...over,
});

const POS: Record<string, { x: number; y: number }> = {
  "web/package.json": { x: 200, y: 300 },
};

function draw(stubs: ProjectStub[], pos = POS) {
  return render(
    <svg>
      <ArrowsOut stubs={stubs} pos={pos} onHover={vi.fn()} />
    </svg>,
  );
}

describe("Arrows out", () => {
  it("attaches the arrow to the node for the file that declares it", () => {
    draw([stub()]);
    const line = document.querySelector('[data-testid="stub-CORE"] line')!;
    expect(line.getAttribute("x1")).toBe("200");
    expect(line.getAttribute("y1")).toBe("300");
  });

  it("draws a project as a square, not another circle", () => {
    // A project is not a file, and the two should not have to be told apart by size.
    draw([stub()]);
    const group = screen.getByTestId("stub-CORE");
    expect(group.querySelector("rect")).toBeTruthy();
    expect(group.querySelector("circle")).toBeNull();
  });

  it("still draws an arrow whose declaring file was never described, and says so", () => {
    // Hiding it would lose a real dependency; drawing it from nowhere with no explanation
    // is what the evidence rule exists to prevent. So: rendered, in a tray, labelled.
    draw([stub({ unanchored: true, anchor_paths: [] })], {});
    const group = screen.getByTestId("stub-CORE");
    expect(group.querySelector("line")).toBeNull(); // nothing to attach to
    expect(group.textContent).toMatch(/no anchor/);
  });

  it("fades a stale arrow rather than dropping it", () => {
    draw([stub({ fresh: false })]);
    expect(screen.getByTestId("stub-CORE").getAttribute("opacity")).toBe("0.5");
  });

  it("renders nothing at all when a project has no siblings", () => {
    // Outside an org there is nothing to depend on. That is an empty list, not a failure,
    // and it must not leave an empty decoration behind.
    draw([]);
    expect(screen.queryByTestId("arrows-out")).not.toBeInTheDocument();
  });
});
