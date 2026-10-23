import { act, render } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import viewSrc from "../features/code/CodeGraphView.tsx?raw";
import { CodeGraphCanvas } from "@/features/code/CodeGraphCanvas";
import {
  CLOUD_ALPHA,
  CLOUD_SOLO_ALPHA,
  CONTENTION_COLOR,
  PULSE_MEAN,
  canvasClouds,
  predictedStrokes,
  type CloudLayout,
} from "@/features/code/canvas/clouds";
import { LOD_ZOOM } from "@/features/code/canvas/edges";
import { buildScene, type DrawScene, type SceneOpts } from "@/features/code/canvas/scene";
import type { ClusterLayout } from "@/lib/graph/bigLayout";
import type { FleetPresence, HeldArea } from "@/lib/types";

/**
 * S5 presence on the canvas (GRPH-1074). `cloudsFor` stays untouched.
 * These tests fail when the canvas stops handing clouds to the scene, or
 * when the scene stops asking `contentionOf` who is on a node.
 */

const drawn = vi.hoisted(() => ({ list: [] as DrawScene[] }));

const layoutBox = vi.hoisted(() => ({ current: null as ClusterLayout | null }));

vi.mock("@/lib/graph/useAreaLayout", () => ({
  useAreaLayout: () => ({
    layout: layoutBox.current,
    pending: false,
    mode: "inline" as const,
    deaths: 0,
    slow: false,
  }),
}));

vi.mock("@/features/code/canvas/webgl", async (orig) => {
  const actual = await orig<typeof import("@/features/code/canvas/webgl")>();
  return { ...actual, createWebGL: () => null };
});

vi.mock("@/features/code/canvas/canvas2d", async (orig) => {
  const actual = await orig<typeof import("@/features/code/canvas/canvas2d")>();
  return {
    ...actual,
    createCanvas2D: () => ({
      kind: "canvas2d" as const,
      draw(scene: DrawScene) {
        drawn.list.push(scene);
      },
      destroy() {},
    }),
  };
});

function held(over: Partial<HeldArea> & Pick<HeldArea, "agent_id" | "user_id" | "node_paths">): HeldArea {
  return {
    area: "mod",
    item_id: null,
    expires_at: null,
    agent_label: over.agent_id ?? "agent",
    active_role: "worker",
    state: "working",
    user_initials: "AD",
    user_color: over.user_id === "bea" ? "#7ca2ff" : "#c6f24e",
    ...over,
  };
}

function presence(rows: HeldArea[]): FleetPresence {
  return {
    served_at: "2026-10-23T00:00:00Z",
    heartbeat_interval_seconds: 50,
    held: rows,
    off_map: [],
    truncated: false,
    total: rows.length,
  };
}

function cloudLayout(specs: { id: string; x: number; y: number; area: string }[]): CloudLayout {
  return {
    ids: specs.map((s) => s.id),
    x: specs.map((s) => s.x),
    y: specs.map((s) => s.y),
    areaOf: specs.map((s) => s.area),
  };
}

function cluster(specs: { id: string; x: number; y: number; area: string; r?: number }[]): ClusterLayout {
  return {
    ids: specs.map((s) => s.id),
    x: Float32Array.from(specs.map((s) => s.x)),
    y: Float32Array.from(specs.map((s) => s.y)),
    r: Float32Array.from(specs.map((s) => s.r ?? 8)),
    kinds: specs.map(() => "file"),
    colors: specs.map(() => "#ffffff"),
    areaOf: specs.map((s) => s.area),
    areas: [],
    bundles: [],
    edges: [],
    grid: {},
    depth: 1,
    nodeCount: specs.length,
  };
}

function sceneOpts(layout: ClusterLayout, extra?: Partial<SceneOpts>): SceneOpts {
  const alpha = new Uint8Array(layout.ids.length);
  alpha.fill(255);
  return {
    camera: { x: 0, y: 0, k: 1 },
    width: 900,
    height: 560,
    dpr: 1,
    alpha,
    types: null,
    isolate: null,
    selectedId: null,
    hoverId: null,
    ...extra,
  };
}

const FAR: { id: string; x: number; y: number; area: string }[] = [
  { id: "a.py", x: 0, y: 0, area: "mod" },
  { id: "b.py", x: 200, y: 0, area: "mod" },
];

describe("canvas clouds", () => {
  it("draws one cloud per user per area when zoomed out, and one per node at fine zoom", () => {
    const layout = cloudLayout(FAR);
    const p = presence([held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py", "b.py"] })]);
    const wide = canvasClouds(layout, p, LOD_ZOOM - 0.001);
    expect(wide).toHaveLength(1);
    expect(wide[0].userId).toBe("ada");
    const fine = canvasClouds(layout, p, LOD_ZOOM);
    expect(fine.map((c) => c.x).sort((a, b) => a - b)).toEqual([0, 200]);
  });

  it("does not merge two areas into one cloud just because the nodes are close", () => {
    const layout = cloudLayout([
      { id: "a.py", x: 0, y: 0, area: "left" },
      { id: "b.py", x: 10, y: 0, area: "right" },
    ]);
    const p = presence([held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py", "b.py"] })]);
    expect(canvasClouds(layout, p, 1)).toHaveLength(2);
  });

  it("keeps both users on one node, and fades the other one under solo instead of dropping it", () => {
    const layout = cloudLayout([{ id: "a.py", x: 4, y: 6, area: "mod" }]);
    const p = presence([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
      held({ agent_id: "b1", user_id: "bea", node_paths: ["a.py"], user_initials: "BE" }),
    ]);
    const both = canvasClouds(layout, p, LOD_ZOOM);
    expect(both).toHaveLength(2);
    expect(both.every((c) => c.alpha === CLOUD_ALPHA)).toBe(true);
    const solo = canvasClouds(layout, p, LOD_ZOOM, "ada");
    expect(solo).toHaveLength(2);
    expect(solo.find((c) => c.userId === "ada")?.alpha).toBe(CLOUD_ALPHA);
    expect(solo.find((c) => c.userId === "bea")?.alpha).toBe(CLOUD_SOLO_ALPHA);
  });

  it("collapses two agents of one user into one cloud", () => {
    const layout = cloudLayout([{ id: "a.py", x: 0, y: 0, area: "mod" }]);
    const p = presence([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
      held({ agent_id: "a2", user_id: "ada", node_paths: ["a.py"] }),
    ]);
    expect(canvasClouds(layout, p, 1)).toHaveLength(1);
  });

  it("returns nothing when nobody looked or nobody holds a node", () => {
    const layout = cloudLayout(FAR);
    expect(canvasClouds(layout, null, 1)).toEqual([]);
    expect(canvasClouds(layout, undefined, 1)).toEqual([]);
    expect(canvasClouds(layout, presence([]), 1)).toEqual([]);
  });

  it("dashes a predicted rim and leaves a touched area solid", () => {
    const layout = cloudLayout([{ id: "a.py", x: 0, y: 0, area: "mod" }]);
    const predicted = presence([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"], predicted: true }),
    ]);
    const solid = presence([held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] })]);
    const dashed = canvasClouds(layout, predicted, 1);
    expect(dashed[0].predicted).toBe(true);
    expect(predictedStrokes(dashed).length).toBeGreaterThan(0);
    expect(predictedStrokes(dashed).every((s) => s.kind === "line")).toBe(true);
    expect(predictedStrokes(canvasClouds(layout, solid, 1))).toEqual([]);
  });
});

describe("contention rings", () => {
  const layout = cluster([{ id: "a.py", x: 40, y: 30, area: "mod", r: 8 }]);

  function ringsFor(rows: HeldArea[], extra?: Partial<SceneOpts>) {
    return buildScene(layout, sceneOpts(layout, { presence: presence(rows), ...extra })).rings;
  }

  it("rings two users at r+3 and ignores two agents of one user", () => {
    const contested = ringsFor([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
      held({ agent_id: "b1", user_id: "bea", node_paths: ["a.py"] }),
    ]);
    const ring = contested.find((r) => r.kind === "contention");
    expect(ring).toMatchObject({ r: 11, color: CONTENTION_COLOR, alpha: 0.9 });
    const same = ringsFor([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
      held({ agent_id: "a2", user_id: "ada", node_paths: ["a.py"] }),
    ]);
    expect(same.filter((r) => r.kind === "contention")).toEqual([]);
  });

  it("treats two unresolved humans as distinct", () => {
    const rings = ringsFor([
      held({ agent_id: "a1", user_id: null, node_paths: ["a.py"] }),
      held({ agent_id: "b1", user_id: null, node_paths: ["a.py"] }),
    ]);
    expect(rings.some((r) => r.kind === "contention")).toBe(true);
  });

  it("nests contention inside the pin and the selection", () => {
    const rings = ringsFor(
      [
        held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
        held({ agent_id: "b1", user_id: "bea", node_paths: ["a.py"] }),
      ],
      { pinned: new Set(["a.py"]), selectedId: "a.py" },
    );
    const byKind = Object.fromEntries(rings.map((r) => [r.kind, r.r]));
    expect(byKind).toEqual({ contention: 11, pin: 13, selection: 15 });
  });

  it("holds the ring at the pulse mean when motion is reduced", () => {
    const scene = buildScene(
      layout,
      sceneOpts(layout, {
        presence: presence([
          held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
          held({ agent_id: "b1", user_id: "bea", node_paths: ["a.py"] }),
        ]),
        reducedMotion: true,
      }),
    );
    expect(scene.staticPresence).toBe(true);
    expect(scene.rings.find((r) => r.kind === "contention")?.alpha).toBe(PULSE_MEAN);
  });

  it("copies the cloud list the canvas computed, and an omitted list is empty", () => {
    const clouds = canvasClouds(
      cloudLayout([{ id: "a.py", x: 40, y: 30, area: "mod" }]),
      presence([held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] })]),
      1,
    );
    const scene = buildScene(layout, sceneOpts(layout, { clouds }));
    expect(scene.clouds).toBe(clouds);
    expect(buildScene(layout, sceneOpts(layout)).clouds).toEqual([]);
  });
});

describe("the view hands presence to the canvas", () => {
  it("passes the live reservations and the legend's solo into the canvas element", () => {
    const at = viewSrc.indexOf("<CodeGraphCanvas");
    const jsx = viewSrc.slice(at, viewSrc.indexOf("/>", at));
    expect(jsx).toContain("presence={presence}");
    expect(jsx).toContain("soloUser={soloUser}");
  });
});

function media(reduce: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: reduce && query.includes("reduce"),
    media: query,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
    onchange: null,
  })) as typeof window.matchMedia;
}

type CanvasProps = ComponentProps<typeof CodeGraphCanvas>;

function mountCanvas(over: Partial<CanvasProps> = {}) {
  const onSelect = vi.fn();
  const props: CanvasProps = {
    nodes: [
      { id: "a.py", path: "a.py", kind: "file", name: "a.py" },
      { id: "b.py", path: "b.py", kind: "file", name: "b.py" },
    ],
    edges: [],
    enabledTypes: ["imports"],
    isolate: false,
    selectedId: null,
    onSelect,
    truncated: false,
    totalNodes: 2,
    frameId: null,
    layout: "clusters",
    onLayout: () => {},
    ...over,
  };
  const view = render(<CodeGraphCanvas {...props} />);
  return { ...view, onSelect, props };
}

describe("the canvas draws the presence it was given", () => {
  let queued: FrameRequestCallback | null = null;
  const previousMedia = window.matchMedia;

  beforeEach(() => {
    drawn.list = [];
    queued = null;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      queued = cb;
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});
    media(true);
    layoutBox.current = cluster([
      { id: "a.py", x: 0, y: 0, area: "mod" },
      { id: "b.py", x: 200, y: 0, area: "mod" },
    ]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.matchMedia = previousMedia;
  });

  function lastScene(): DrawScene {
    const scene = drawn.list.at(-1);
    expect(scene).toBeTruthy();
    return scene!;
  }

  it("puts the owner's cloud on the frame the fallback renderer draws", () => {
    const p = presence([held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] })]);
    mountCanvas({ presence: p });
    const scene = lastScene();
    expect(scene.clouds?.some((c) => c.userId === "ada" && c.alpha === CLOUD_ALPHA)).toBe(true);
    expect(scene.rings.some((r) => r.kind === "contention")).toBe(false);
  });

  it("shows the contention ring for two users and not for two agents of one user", () => {
    const two = presence([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
      held({ agent_id: "b1", user_id: "bea", node_paths: ["a.py"] }),
    ]);
    const { rerender, props } = mountCanvas({ presence: two });
    const contested = lastScene();
    expect(contested.rings.find((r) => r.kind === "contention")).toMatchObject({
      r: 11,
      color: CONTENTION_COLOR,
      alpha: PULSE_MEAN,
    });
    expect(contested.staticPresence).toBe(true);

    drawn.list = [];
    const same = presence([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
      held({ agent_id: "a2", user_id: "ada", node_paths: ["a.py"] }),
    ]);
    rerender(<CodeGraphCanvas {...props} presence={same} />);
    expect(lastScene().rings.some((r) => r.kind === "contention")).toBe(false);
    expect(lastScene().clouds).toHaveLength(1);
  });

  it("dims the other user's nodes when the legend solos, and keeps both clouds", () => {
    const p = presence([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
      held({ agent_id: "b1", user_id: "bea", node_paths: ["b.py"] }),
    ]);
    mountCanvas({ presence: p, soloUser: "ada" });
    const scene = lastScene();
    expect(scene.nodes.find((n) => n.id === "a.py")?.alpha).toBe(1);
    expect(scene.nodes.find((n) => n.id === "b.py")?.alpha).toBeCloseTo(0.22, 2);
    expect(scene.clouds?.find((c) => c.userId === "ada")?.alpha).toBe(CLOUD_ALPHA);
    expect(scene.clouds?.find((c) => c.userId === "bea")?.alpha).toBe(CLOUD_SOLO_ALPHA);
  });

  it("pulses the contention ring when motion is allowed", () => {
    media(false);
    const p = presence([
      held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"] }),
      held({ agent_id: "b1", user_id: "bea", node_paths: ["a.py"] }),
    ]);
    mountCanvas({ presence: p });
    expect(drawn.list).toHaveLength(0);
    act(() => {
      queued?.(0);
    });
    const ring = lastScene().rings.find((r) => r.kind === "contention");
    expect(ring?.alpha).toBeCloseTo(0.85, 5);
  });
});

describe("canvas 2d paints the cloud layer", () => {
  const original = HTMLCanvasElement.prototype.getContext;

  afterEach(() => {
    HTMLCanvasElement.prototype.getContext = original;
  });

  it("builds a radial fill for each cloud the scene carries", async () => {
    const { createCanvas2D } = await vi.importActual<typeof import("@/features/code/canvas/canvas2d")>(
      "@/features/code/canvas/canvas2d",
    );
    let gradients = 0;
    const ctx = {
      createRadialGradient() {
        gradients += 1;
        return { addColorStop() {} };
      },
      setTransform() {},
      clearRect() {},
      save() {},
      restore() {},
      beginPath() {},
      closePath() {},
      moveTo() {},
      lineTo() {},
      arc() {},
      fill() {},
      stroke() {},
      fillRect() {},
      translate() {},
      setLineDash() {},
      fillStyle: "",
      strokeStyle: "",
      lineWidth: 1,
      globalAlpha: 1,
      shadowColor: "",
      shadowBlur: 0,
    };
    HTMLCanvasElement.prototype.getContext = function (type: string) {
      if (type === "2d") return ctx as unknown as CanvasRenderingContext2D;
      return null;
    } as typeof original;
    const layout = cluster([{ id: "a.py", x: 40, y: 30, area: "mod" }]);
    const clouds = canvasClouds(
      cloudLayout([{ id: "a.py", x: 40, y: 30, area: "mod" }]),
      presence([held({ agent_id: "a1", user_id: "ada", node_paths: ["a.py"], predicted: true })]),
      1,
    );
    const scene = buildScene(layout, sceneOpts(layout, { clouds }));
    const renderer = createCanvas2D(document.createElement("canvas"));
    expect(renderer?.kind).toBe("canvas2d");
    renderer!.draw(scene);
    expect(gradients).toBe(clouds.length);
    expect(gradients).toBeGreaterThan(0);
  });
});
