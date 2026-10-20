import { describe, expect, it } from "vitest";

import { buildStackLayout } from "@/lib/stack/layout";
import type { StackNode, StackTopology } from "@/lib/types";

/** Minimal node with the fields buildStackLayout reads. */
function node(p: Partial<StackNode> & Pick<StackNode, "id" | "ring" | "host">): StackNode {
  return {
    label: p.id!,
    title: p.id!,
    kind: "deployable",
    scope: "instance",
    liveness: "unknown",
    surfaces: [],
    extensions: [],
    facts: [],
    ...p,
  } as StackNode;
}

const topo: StackTopology = {
  project_id: "core",
  generated_at: "2026-10-19T00:00:00Z",
  hosts: [
    { id: "compose", label: "Compose / Railway stack", nodes: ["web", "api", "db"] },
    { id: "user_machines", label: "User machines", nodes: ["cli"] },
  ],
  nodes: [
    // api carries all four surfaces — the box the view used to blow out on.
    node({
      id: "api",
      ring: 1,
      host: "compose",
      liveness: "reachable",
      surfaces: ["REST", "MCP", "sync engine", "provider-resolution"],
    }),
    node({ id: "web", ring: 1, host: "compose" }),
    node({ id: "db", ring: 2, host: "compose", kind: "infra", extensions: ["pgvector"] }),
    node({ id: "cli", ring: 1, host: "user_machines" }),
  ],
  edges: [{ from: "web", to: "api", protocol: "HTTP", label: "HTTP" }],
  empties: [],
  catalog: { providers_total: 0, providers_wired: 0, unwired_count: 0 },
  identity: {} as StackTopology["identity"],
};

describe("buildStackLayout — box sizing stays condensed", () => {
  const { boxes } = buildStackLayout(topo);

  it("does not let api blow out: it is no wider than the db box, not a 320px bulge", () => {
    // api used to be a hardcoded 320x180 tile box, far wider than its column
    // siblings, which stretched the whole canvas. It now sizes like other
    // surface/extension boxes.
    expect(boxes.api.w).toBe(boxes.db.w);
    expect(boxes.api.w).toBeLessThanOrEqual(250);
  });

  it("reserves code-health footer height on ring-1 boxes so content never clips", () => {
    // Every ring-1 deployable shows the GRPH-1035 health footer; its height must
    // be in the box or the footer overlaps the content. A bare ring-1 box (web)
    // is taller than the pre-footer base of 104.
    expect(boxes.web.h).toBeGreaterThan(104);
    // api's four surfaces wrap to a second line, so it is the tallest — but only
    // modestly, not the old 180.
    expect(boxes.api.h).toBeGreaterThan(boxes.web.h);
    expect(boxes.api.h).toBeLessThanOrEqual(180);
  });
});

describe("buildStackLayout — the outpost list never blows the box out vertically", () => {
  function outposts(n: number) {
    return Array.from({ length: n }, (_, i) => ({
      host: `agent-host-${i}`,
      online: 0,
      agents: 1,
    }));
  }
  function fleetTopo(n: number): StackTopology {
    return {
      ...topo,
      nodes: [node({ id: "fleet", ring: 1, host: "user_machines", outposts: outposts(n) })],
      edges: [],
    };
  }

  it("a fleet node with hundreds of hosts sizes the same as one at the preview cap", () => {
    // The fleet node lists one row per agent host; a busy instance has dozens
    // (retired waves included). Rendering them all grew the box 20px a row until
    // it was thousands of px tall and the map shrank to a speck (seen on Super-Arc).
    const few = buildStackLayout(fleetTopo(4)).boxes.fleet.h;
    const many = buildStackLayout(fleetTopo(400)).boxes.fleet.h;
    // 400 hosts collapse to the preview rows + one "+N more" line, so the box is
    // only modestly taller than the 4-host box — never 400 rows tall (that was
    // ~8000px). One "+N more" line (~18px) is the whole difference.
    expect(many).toBeLessThanOrEqual(few + 20);
    expect(many).toBeLessThanOrEqual(240);
  });

  it("is monotonic but bounded: 1000 hosts is no taller than 5", () => {
    expect(buildStackLayout(fleetTopo(1000)).boxes.fleet.h).toBe(
      buildStackLayout(fleetTopo(5)).boxes.fleet.h,
    );
  });
});

describe("buildStackLayout — ownership rings nest and clear the boxes", () => {
  const { rings, boxes, hostFrames } = buildStackLayout(topo);
  const contains = (o: { x: number; y: number; w: number; h: number }, i: typeof o) =>
    o.x <= i.x && o.y <= i.y && o.x + o.w >= i.x + i.w && o.y + o.h >= i.y + i.h;
  const ring = (n: number) => rings.find((r) => r.ring === n)!;

  it("draws all three rings, outermost first", () => {
    expect(rings.map((r) => r.ring)).toEqual([3, 2, 1]);
  });

  it("nests ring1 ⊂ ring2 ⊂ ring3, each strictly inside the next", () => {
    expect(contains(ring(3), ring(2))).toBe(true);
    expect(contains(ring(2), ring(1))).toBe(true);
    // The top edges stagger upward so each label has its own clear line.
    expect(ring(3).y).toBeLessThan(ring(2).y);
    expect(ring(2).y).toBeLessThan(ring(1).y);
  });

  it("ring1 (code we write) sits OUTSIDE the host frames, never crossing a box", () => {
    // The old bug: ring1 pad (20) < host pad (22), so the code ring cut between the
    // host frame and the boxes. Ring1 must now contain every host frame whose nodes
    // are all ring-1, and every ring-1 box with margin.
    const codeBox = boxes.cli; // cli is a lone ring-1 box on its own host
    expect(contains(ring(1), codeBox)).toBe(true);
    const userHost = hostFrames.find((h) => h.id === "user_machines")!;
    expect(contains(ring(1), userHost)).toBe(true);
  });
});

describe("buildStackLayout — edge routing does not pile parallel edges on one port", () => {
  // web calls three externals. Center-to-center routing left all three on web's
  // right-face centre, overlapping the lines and stacking their labels into one
  // run ("HTTP HTTP chat"). Ports must now be distributed along the face.
  const fan: StackTopology = {
    ...topo,
    hosts: [
      { id: "compose", label: "Compose", nodes: ["web"] },
      { id: "external", label: "External", nodes: ["xai", "ollama", "qwen"] },
    ],
    nodes: [
      node({ id: "web", ring: 1, host: "compose" }),
      node({ id: "xai", ring: 3, host: "external", kind: "external" }),
      node({ id: "ollama", ring: 3, host: "external", kind: "external" }),
      node({ id: "qwen", ring: 3, host: "external", kind: "external" }),
    ],
    edges: [
      { from: "web", to: "xai", protocol: "chat", label: "chat" },
      { from: "web", to: "ollama", protocol: "chat", label: "chat" },
      { from: "web", to: "qwen", protocol: "chat", label: "chat" },
    ],
  };
  const { edges, boxes } = buildStackLayout(fan);

  it("gives each edge leaving web a distinct exit point", () => {
    const exitYs = edges.map((e) => e.points[0].y);
    expect(new Set(exitYs).size).toBe(edges.length);
    // All exit on web's right face (within its height), never the bare centre.
    for (const e of edges) {
      expect(e.points[0].x).toBeCloseTo(boxes.web.x + boxes.web.w, 3);
      expect(e.points[0].y).toBeGreaterThan(boxes.web.y);
      expect(e.points[0].y).toBeLessThan(boxes.web.y + boxes.web.h);
    }
  });

  it("separates the protocol-label anchors so they no longer overlap", () => {
    const mids = edges.map((e) => `${Math.round(e.mid.x)},${Math.round(e.mid.y)}`);
    expect(new Set(mids).size).toBe(edges.length);
    // The label sits in the box-free channel to the right of web, not inside it.
    for (const e of edges) expect(e.mid.x).toBeGreaterThan(boxes.web.x + boxes.web.w);
  });

  it("still emits exactly one route per topology edge, arrow head last", () => {
    expect(edges.length).toBe(fan.edges.length);
    for (const e of edges) {
      const last = e.points[e.points.length - 1];
      // Arrow head lands on the target's left face.
      expect(last.x).toBeCloseTo(boxes[e.to].x, 3);
    }
  });
});
