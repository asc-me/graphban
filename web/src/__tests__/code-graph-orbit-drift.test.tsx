import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodeGraphCanvas } from "@/features/code/CodeGraphCanvas";

// A chain of modules, each owning ten files, so Orbit puts areas on several rings.
const mods = ["data", "l1", "l2", "l3", "docs"];
const nodes = mods.flatMap((m) => [
  { id: m, path: m, kind: "module", name: m },
  ...Array.from({ length: 10 }, (_, i) => ({ id: `${m}/f${i}`, path: `${m}/f${i}.py`, kind: "file", name: `f${i}.py` })),
]);
const edges = [
  ...mods.flatMap((m) => Array.from({ length: 10 }, (_, i) => ({ src: m, dst: `${m}/f${i}`, type: "owns" }))),
  { src: "l1/f0", dst: "data/f0", type: "imports" },
  { src: "l2/f0", dst: "l1/f0", type: "imports" },
  { src: "l3/f0", dst: "l2/f0", type: "imports" },
  { src: "l1/f0", dst: "docs/f0", type: "references" },
];

function mount() {
  return render(
    <CodeGraphCanvas
      nodes={nodes}
      edges={edges}
      enabledTypes={["imports", "calls", "owns", "tested_by", "references"]}
      isolate={false}
      selectedId={null}
      onSelect={() => {}}
      truncated={false}
      totalNodes={nodes.length}
      frameId={null}
    />,
  );
}

function hullXs(): string {
  return [...screen.getByTestId("hull-shapes").querySelectorAll("circle")]
    .map((c) => `${c.getAttribute("cx")},${c.getAttribute("cy")}`)
    .join(" ");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("orbit drift", () => {
  it("turns the rings until paused, and holds still once paused", async () => {
    mount();
    fireEvent.click(screen.getByTestId("layout-chip-orbit"));
    const canvas = screen.getByTestId("code-graph-canvas");
    const toggle = await screen.findByTestId("orbit-motion");
    expect(toggle).toHaveTextContent("Pause orbit");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(canvas).toHaveAttribute("data-drift", "1"));

    const start = hullXs();
    await waitFor(() => expect(hullXs()).not.toBe(start), { timeout: 2000 });

    fireEvent.click(toggle);
    expect(toggle).toHaveTextContent("Resume orbit");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(canvas).toHaveAttribute("data-drift", "0");
    const held = hullXs();
    await act(() => new Promise((r) => setTimeout(r, 200)));
    expect(hullXs()).toBe(held);

    fireEvent.click(toggle);
    expect(canvas).toHaveAttribute("data-drift", "1");
  });

  it("does not move, and offers no toggle, under reduced motion", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: q.includes("reduce"),
      media: q,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
    }));
    mount();
    fireEvent.click(screen.getByTestId("layout-chip-orbit"));
    await screen.findByTestId("hull-shapes");
    expect(screen.queryByTestId("orbit-motion")).not.toBeInTheDocument();
    expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-drift", "0");
  });

  it("only drifts on Orbit", async () => {
    mount();
    await screen.findByTestId("hull-shapes");
    expect(screen.queryByTestId("orbit-motion")).not.toBeInTheDocument();
    expect(screen.getByTestId("code-graph-canvas")).toHaveAttribute("data-drift", "0");
  });
});
