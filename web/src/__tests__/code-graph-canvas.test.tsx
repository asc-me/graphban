import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CodeGraphCanvas } from "@/features/code/CodeGraphCanvas";

const node = { id: "alpha/a.py", path: "alpha/a.py", kind: "file", name: "a.py" };

function mount(truncated: boolean, total = 1) {
  render(
    <CodeGraphCanvas
      nodes={[node]}
      edges={[]}
      enabledTypes={["imports", "calls", "owns", "tested_by", "references"]}
      isolate={false}
      selectedId={null}
      onSelect={() => {}}
      truncated={truncated}
      totalNodes={total}
      frameId={null}
    />,
  );
}

describe("canvas chrome", () => {
  it("shows the truncated caption only when the map is a page", () => {
    mount(true, 12000);
    expect(screen.getByTestId("truncated-caption")).toHaveTextContent(
      "Showing 1 of 12,000 nodes (truncated)",
    );
  });

  it("stays quiet when the server says the map is complete", () => {
    mount(false, 1);
    expect(screen.queryByTestId("truncated-caption")).not.toBeInTheDocument();
  });

  it("enables Clusters, Map, Orbit and Matrix", async () => {
    mount(false);
    expect(screen.getByTestId("layout-chip-clusters")).toHaveAttribute("aria-pressed", "true");
    const map = screen.getByTestId("layout-chip-map");
    const orbit = screen.getByTestId("layout-chip-orbit");
    const matrix = screen.getByTestId("layout-chip-matrix");
    expect(map).toBeEnabled();
    expect(orbit).toBeEnabled();
    expect(matrix).toBeEnabled();
    expect(map).toHaveAttribute("aria-pressed", "false");
    expect(orbit).toHaveAttribute("aria-pressed", "false");
    expect(matrix).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(map);
    expect(map).toHaveAttribute("aria-pressed", "true");
    expect(orbit).toHaveAttribute("aria-pressed", "false");
    expect(await screen.findByTestId("lod-caption")).toHaveTextContent(
      "Hover an area to see its dependencies",
    );
  });
});
