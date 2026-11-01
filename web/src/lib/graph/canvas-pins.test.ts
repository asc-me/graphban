import { describe, expect, it } from "vitest";

import { fitCamera } from "@/features/code/canvas/camera";
import { buildScene } from "@/features/code/canvas/scene";
import {
  applyPinOverrides,
  fitTargets,
  hitTest,
  type ClusterLayout,
} from "@/lib/graph/bigLayout";
import { dropRegrouped, pinTotal, pinsFor, withPin } from "@/lib/graph/useGraphPins";

function layoutAtOrigin(): ClusterLayout {
  return {
    ids: ["a"],
    x: Float32Array.from([0]),
    y: Float32Array.from([0]),
    r: Float32Array.from([6]),
    kinds: ["file"],
    colors: ["#fff"],
    areaOf: ["A"],
    areas: [{ id: "A", cx: 0, cy: 0, r: 40, color: "#111", count: 1, label: "A" }],
    bundles: [],
    edges: [],
    grid: { "0,0": [0] },
    depth: 1,
    nodeCount: 1,
  };
}

const sceneOpts = (pinned?: ReadonlySet<string>) => ({
  camera: { x: 0, y: 0, k: 1 },
  width: 800,
  height: 600,
  dpr: 1,
  alpha: new Uint8Array([255]),
  types: null,
  isolate: null,
  selectedId: null,
  hoverId: null,
  pinned,
});

describe("canvas pin overrides", () => {
  it("moves the node into its new grid cell and leaves the cached layout alone", () => {
    const layout = layoutAtOrigin();
    const cachedX = layout.x[0];
    const cachedBucket = layout.grid["0,0"];
    const pinned = applyPinOverrides(layout, { a: { x: 80, y: 0 } });

    expect(layout.x[0]).toBe(cachedX);
    expect(layout.grid["0,0"]).toBe(cachedBucket);
    expect(layout.grid["0,0"]).toEqual([0]);
    expect(pinned).not.toBe(layout);
    expect(pinned.x[0]).toBe(80);
    expect(pinned.grid["0,0"] ?? []).not.toContain(0);
    expect(pinned.grid["8,0"]).toEqual([0]);
    expect(hitTest(pinned, 80, 0)).toBe("a");
    expect(hitTest(pinned, 0, 0)).toBeNull();
    expect(hitTest(layout, 0, 0)).toBe("a");
  });

  it("keeps a pin on its layout when another layout is showing, and restores it", () => {
    const layout = layoutAtOrigin();
    const all = withPin({}, "clusters", "a", { x: 80, y: 0, areaKey: "A" });
    expect(pinsFor(all, "map")).toEqual({});
    expect(applyPinOverrides(layout, pinsFor(all, "map"))).toBe(layout);
    const back = applyPinOverrides(layout, pinsFor(all, "clusters"));
    expect(hitTest(back, 80, 0)).toBe("a");
    expect(hitTest(layout, 80, 0)).toBeNull();
  });

  it("draws the pin ring at r+5 on the overridden position", () => {
    const pinned = applyPinOverrides(layoutAtOrigin(), { a: { x: 40, y: 12 } });
    const scene = buildScene(pinned, sceneOpts(new Set(["a"])));
    const ring = scene.rings.find((r) => r.kind === "pin");
    expect(ring).toMatchObject({ x: 40, y: 12, r: pinned.r[0] + 5, kind: "pin" });
    expect(buildScene(pinned, sceneOpts()).rings.filter((r) => r.kind === "pin")).toEqual([]);
  });

  it("drops a pin whose area key changed and counts only those", () => {
    const both = withPin(
      withPin({}, "clusters", "a", { x: 10, y: 0, areaKey: "A" }),
      "map",
      "a",
      { x: 12, y: 0, areaKey: "A" },
    );
    const regrouped = dropRegrouped(both, () => "B");
    expect(regrouped.cleared).toBe(2);
    expect(pinTotal(regrouped.pins)).toBe(0);
    expect(dropRegrouped(regrouped.pins, () => "B").pins).toBe(regrouped.pins);

    const withGhost = withPin(both, "clusters", "b", { x: 1, y: 1, areaKey: "gone" });
    const droppedGhost = dropRegrouped(withGhost, (id) => (id === "b" ? undefined : "A"));
    expect(droppedGhost.cleared).toBe(0);
    expect(pinsFor(droppedGhost.pins, "clusters").a?.areaKey).toBe("A");
    expect(pinsFor(droppedGhost.pins, "clusters").b).toBeUndefined();
    expect(pinsFor(droppedGhost.pins, "map").a?.areaKey).toBe("A");
  });

  it("includes a pin outside its hull in the fit", () => {
    const layout = layoutAtOrigin();
    const hullOnly = fitCamera(
      layout.areas.map((a) => ({ x: a.cx, y: a.cy, r: a.r })),
      900,
      560,
    );
    const outside = 500 * hullOnly.k + hullOnly.x;
    expect(outside).toBeGreaterThan(900);
    const cam = fitCamera(fitTargets(layout, { a: { x: 500, y: 0 } }), 900, 560);
    const sx = 500 * cam.k + cam.x;
    expect(sx).toBeGreaterThan(0);
    expect(sx).toBeLessThan(900);
  });
});
