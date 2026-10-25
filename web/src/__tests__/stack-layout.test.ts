import { describe, expect, it } from "vitest";

import {
  BUILD_LANE,
  buildStackLayout,
  GRAPHBAN_BOX,
  prepareStackMap,
  SERVICES_NOT_MAPPED,
  UNMAPPED_BOX,
} from "@/lib/stack/layout";
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

  it("carries each ring label as a badge number, title and subtitle (not one flat string)", () => {
    // D: the header renders a circled number + uppercase title + plain-language
    // subtitle, so the layout must expose the parts rather than a joined label.
    expect(ring(1).num).toBe(1);
    expect(ring(1).title).toBe("CODE WE WRITE");
    expect(ring(1).subtitle).toMatch(/deployables/);
    expect(ring(2).title).toBe("INFRASTRUCTURE WE RUN");
    expect(ring(3).title).toBe("EXTERNAL SERVICES");
    // The title is an uppercase phrase with no leading "N · " ordinal baked in.
    for (const r of rings) {
      expect(r.title).toBe(r.title.toUpperCase());
      expect(r.title).not.toMatch(/·/);
    }
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

describe("buildStackLayout — a ring-2 (infra) box never straddles the ring-1 line", () => {
  // db is infra we run (ring 2); it must sit in the ring-2 band, OUTSIDE the lime
  // "code we write" ring — never half in, half out. The bug: db stacked directly
  // under web in the compose column, but a taller sibling column (agent hosts,
  // with the fleet node's outpost rows) dragged ring-1's bounding box down past
  // db's top, so the ring-1 frame edge cut straight through db.
  const tall: StackTopology = {
    ...topo,
    hosts: [
      { id: "agent_hosts", label: "Agent hosts", nodes: ["fleet", "worker"] },
      { id: "compose", label: "Compose", nodes: ["api", "db"] },
    ],
    nodes: [
      // fleet is tall (many outpost rows) so the agent column runs well below db.
      node({
        id: "fleet",
        ring: 1,
        host: "agent_hosts",
        outposts: Array.from({ length: 40 }, (_, i) => ({ host: `h${i}`, online: 0, agents: 1 })),
      }),
      node({ id: "worker", ring: 1, host: "agent_hosts" }),
      node({ id: "api", ring: 1, host: "compose" }),
      node({ id: "db", ring: 2, host: "compose", kind: "infra", extensions: ["pgvector"] }),
    ],
    edges: [{ from: "api", to: "db", protocol: "sql", label: "sql" }],
  };
  const { boxes, rings } = buildStackLayout(tall);
  const ring1 = rings.find((r) => r.ring === 1)!;

  it("places db entirely below the ring-1 frame's bottom edge", () => {
    expect(boxes.db.y).toBeGreaterThanOrEqual(ring1.y + ring1.h);
  });

  it("keeps every ring-1 box inside the ring-1 frame (db is the only one outside)", () => {
    for (const id of ["fleet", "worker", "api"]) {
      expect(boxes[id].y).toBeGreaterThanOrEqual(ring1.y);
      expect(boxes[id].y + boxes[id].h).toBeLessThanOrEqual(ring1.y + ring1.h);
    }
  });
});

describe("buildStackLayout — no edge segment runs under a box it does not touch", () => {
  // api sits at the TOP of its column; laya is the LAST external, at the bottom of
  // the right-hand column. Centre-distance routing flipped such an edge to the
  // top/bottom faces once the vertical drop beat the horizontal gap, sending the
  // mid-segment slicing across web and dropping the entry through the box directly
  // above laya. Routing from box extents keeps it right→left, in the gap.
  const deep: StackTopology = {
    ...topo,
    hosts: [
      { id: "compose", label: "Compose", nodes: ["api", "web", "db"] },
      { id: "external", label: "External", nodes: ["xai", "ollama", "qwen", "laya"] },
    ],
    nodes: [
      node({ id: "api", ring: 1, host: "compose", surfaces: ["REST", "MCP"] }),
      node({ id: "web", ring: 1, host: "compose" }),
      node({ id: "db", ring: 2, host: "compose", kind: "infra" }),
      node({ id: "xai", ring: 3, host: "external", kind: "external" }),
      node({ id: "ollama", ring: 3, host: "external", kind: "external" }),
      node({ id: "qwen", ring: 3, host: "external", kind: "external" }),
      node({ id: "laya", ring: 3, host: "external", kind: "external" }),
    ],
    edges: [
      { from: "api", to: "xai", protocol: "chat", label: "chat" },
      { from: "api", to: "ollama", protocol: "chat", label: "chat" },
      { from: "api", to: "qwen", protocol: "chat", label: "chat" },
      { from: "api", to: "laya", protocol: "decide", label: "/v1/systemone" },
      { from: "web", to: "db", protocol: "sql", label: "sql" },
    ],
  };
  const { edges, boxes } = buildStackLayout(deep);

  // Does an axis-aligned segment pass through a box's interior (2px tolerance)?
  const hits = (a: { x: number; y: number }, b: { x: number; y: number }, bx: typeof boxes.api) => {
    const x0 = bx.x + 2,
      x1 = bx.x + bx.w - 2,
      y0 = bx.y + 2,
      y1 = bx.y + bx.h - 2;
    if (Math.abs(a.y - b.y) < 0.5) {
      return a.y > y0 && a.y < y1 && Math.max(a.x, b.x) > x0 && Math.min(a.x, b.x) < x1;
    }
    if (Math.abs(a.x - b.x) < 0.5) {
      return a.x > x0 && a.x < x1 && Math.max(a.y, b.y) > y0 && Math.min(a.y, b.y) < y1;
    }
    return false;
  };

  it("routes the deep api→laya edge without crossing any other box", () => {
    for (const e of edges) {
      for (let i = 0; i < e.points.length - 1; i++) {
        for (const [id, bx] of Object.entries(boxes)) {
          if (id === e.from || id === e.to) continue;
          expect(
            hits(e.points[i], e.points[i + 1], bx),
            `edge ${e.from}->${e.to} segment ${i} crosses ${id}`,
          ).toBe(false);
        }
      }
    }
  });

  it("enters every external provider from its left face", () => {
    for (const e of edges.filter((x) => ["xai", "ollama", "qwen", "laya"].includes(x.to))) {
      const last = e.points[e.points.length - 1];
      expect(last.x).toBeCloseTo(boxes[e.to].x, 3);
    }
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

describe("buildStackLayout — a project that writes none of Graphban", () => {
  // The usual case outside Graphban's own project: every Graphban box is ring 2.
  const runOnly: StackTopology = {
    ...topo,
    nodes: topo.nodes.map((n) => ({ ...n, ring: n.ring === 1 ? 2 : n.ring })),
  };
  const layout = buildStackLayout(runOnly);

  it("draws no code-we-write ring, and still frames the infra and the canvas", () => {
    expect(layout.rings.map((r) => r.ring).sort()).toEqual([2, 3]);
  });

  it("places every box at a finite position with no overlap", () => {
    const boxes = Object.values(layout.boxes);
    expect(boxes).toHaveLength(runOnly.nodes.length);
    for (const b of boxes) {
      for (const v of [b.x, b.y, b.w, b.h]) expect(Number.isFinite(v)).toBe(true);
    }
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i];
        const c = boxes[j];
        const overlap = a.x < c.x + c.w && c.x < a.x + a.w && a.y < c.y + c.h && c.y < a.y + a.h;
        expect(overlap).toBe(false);
      }
    }
  });
});

describe("buildStackLayout — the project's own app", () => {
  const withApp: StackTopology = {
    ...topo,
    hosts: [
      { id: "project_app", label: "Republiq — its own app (from the code map)", nodes: ["app:frontend"] },
      ...topo.hosts,
    ],
    nodes: [
      ...topo.nodes.map((n) => ({ ...n, ring: n.ring === 1 ? 2 : n.ring })),
      node({ id: "app:frontend", ring: 1, host: "project_app", scope: "project" }),
    ],
  };
  const layout = buildStackLayout(withApp);

  it("puts the project's own column first, inside the code-we-write ring", () => {
    const own = layout.boxes["app:frontend"];
    for (const [id, b] of Object.entries(layout.boxes)) {
      if (id !== "app:frontend") expect(own.x).toBeLessThan(b.x);
    }
    const ring1 = layout.rings.find((r) => r.ring === 1)!;
    expect(ring1.x).toBeLessThanOrEqual(own.x);
    expect(ring1.x + ring1.w).toBeGreaterThanOrEqual(own.x + own.w);
    expect(layout.hostFrames.find((h) => h.id === "project_app")?.label).toMatch(/its own app/);
  });
});

describe("buildStackLayout — infra beside the project's own app is not pushed below it", () => {
  // Republiq on 2026.10.15: three own boxes in their column, every Graphban box ring 2.
  // Dropping all ring-2 boxes below the ring-1 frame drew a tall staircase.
  const republiq: StackTopology = {
    ...topo,
    hosts: [
      { id: "project_app", label: "Republiq — its own app", nodes: ["app:backend", "app:frontend", "app:scraper"] },
      ...topo.hosts,
    ],
    nodes: [
      node({ id: "app:backend", ring: 1, host: "project_app", scope: "project" }),
      node({ id: "app:frontend", ring: 1, host: "project_app", scope: "project" }),
      node({ id: "app:scraper", ring: 1, host: "project_app", scope: "project" }),
      ...topo.nodes.map((n) => ({ ...n, ring: n.ring === 1 ? 2 : n.ring })),
    ],
  };
  const layout = buildStackLayout(republiq);
  const ring1 = layout.rings.find((r) => r.ring === 1)!;
  const top = layout.boxes["app:backend"].y;

  it("starts each column beside the code ring at the same top as the code ring", () => {
    expect(layout.boxes["cli"].y).toBe(top);
    expect(layout.boxes["api"].y).toBe(top);
  });

  it("still keeps every infra box out of the code ring's frame", () => {
    for (const n of republiq.nodes.filter((x) => x.ring === 2)) {
      const b = layout.boxes[n.id];
      const inside = b.x < ring1.x + ring1.w && ring1.x < b.x + b.w && b.y < ring1.y + ring1.h && ring1.y < b.y + b.h;
      expect(inside, n.id).toBe(false);
    }
  });

  it("is no taller than the tallest column needs", () => {
    const bottom = Math.max(...Object.values(layout.boxes).map((b) => b.y + b.h));
    const app = ["app:backend", "app:frontend", "app:scraper"].map((id) => layout.boxes[id]);
    const appBottom = Math.max(...app.map((b) => b.y + b.h));
    const compose = ["api", "web", "db"].map((id) => layout.boxes[id]);
    const composeBottom = Math.max(...compose.map((b) => b.y + b.h));
    expect(bottom).toBe(Math.max(appBottom, composeBottom));
  });
});


// ── GRPH-1099 / PRD-P55 §4: app-first map, collapsed Graphban ──────────────────

/** A Republiq-shaped project: a posted record, Graphban run but not written. */
function republiqRecord(): StackTopology {
  const app = (p: Partial<StackNode> & Pick<StackNode, "id" | "ring" | "host">) =>
    node({ scope: "project", plane: "app", ...p });
  return {
    ...topo,
    project_id: "republiq",
    graphban_source: "not_owned",
    hosts: [
      { id: "project_app", label: "republiq — its own app", nodes: ["app:backend", "app:frontend"] },
      { id: "app_infra", label: "republiq — infrastructure", nodes: ["infra:redis", "infra:pg"] },
      { id: "compose", label: "Compose / Railway stack", nodes: ["web", "api", "db"] },
      { id: "user_machines", label: "User machines", nodes: ["cli"] },
      { id: "external", label: "External services", nodes: ["ext:stripe", "unresolved:FOO_URL", "openai"] },
    ],
    nodes: [
      app({ id: "app:frontend", ring: 1, host: "project_app" }),
      app({ id: "app:backend", ring: 1, host: "project_app", provenance: { file: "backend/Dockerfile", rule: "dockerfile" } }),
      app({ id: "infra:redis", ring: 2, host: "app_infra", kind: "infra" }),
      app({ id: "infra:pg", ring: 2, host: "app_infra", kind: "infra" }),
      app({ id: "ext:stripe", ring: 3, host: "external", kind: "external" }),
      app({ id: "unresolved:FOO_URL", ring: 3, host: "external", kind: "unresolved" }),
      ...topo.nodes.map((n) => ({ ...n, plane: "graphban", ring: n.ring === 1 ? 2 : n.ring })),
      node({ id: "openai", ring: 3, host: "external", kind: "external", plane: "graphban" }),
    ],
    edges: [
      { from: "app:frontend", to: "app:backend", protocol: "HTTP", label: "HTTP" },
      { from: "app:backend", to: "infra:pg", protocol: "psycopg", label: "SQL" },
      { from: "app:backend", to: "infra:redis", protocol: "redis", label: "cache" },
      { from: "app:backend", to: "ext:stripe", protocol: "HTTPS", label: "charges" },
      { from: "web", to: "api", protocol: "HTTP", label: "HTTP" },
      { from: "api", to: "openai", protocol: "HTTPS", label: "chat" },
    ],
    mapping: {
      state: "mapped", mapped_sha: "a".repeat(40), head_sha: "a".repeat(40),
      previous_mapped_sha: "", producer_id: "gban", producer_version: "0.4.0",
      mapped_at: null, posted_by: "key:x",
    },
  };
}

describe("prepareStackMap — a project that runs Graphban but does not write it", () => {
  const view = prepareStackMap(republiqRecord());
  const ids = view.topo.nodes.map((n) => n.id);
  const layout = buildStackLayout(view.topo, view.layoutOptions);

  it("collapses the whole instance into one box, providers included", () => {
    expect(view.collapsible).toBe(true);
    expect(view.collapsed).toBe(true);
    expect(ids).toContain(GRAPHBAN_BOX);
    for (const gid of ["api", "web", "db", "cli", "openai"]) expect(ids).not.toContain(gid);
    const box = view.topo.nodes.find((n) => n.id === GRAPHBAN_BOX)!;
    expect(box.members).toEqual(expect.arrayContaining(["api", "web", "db", "cli", "openai"]));
    // No edge survives with a hidden end.
    for (const e of view.topo.edges) {
      expect(ids, `${e.from}->${e.to}`).toContain(e.from);
      expect(ids, `${e.from}->${e.to}`).toContain(e.to);
    }
  });

  it("draws a builds edge to every ring-1 deployable and to nothing else", () => {
    const builds = view.topo.edges.filter((e) => e.protocol === "builds");
    expect(builds.every((e) => e.from === GRAPHBAN_BOX)).toBe(true);
    expect(builds.map((e) => e.to).sort()).toEqual(["app:backend", "app:frontend"]);
  });

  it("puts the collapsed box in a build lane at the left edge, the app beside it", () => {
    const box = layout.boxes[GRAPHBAN_BOX];
    const others = Object.entries(layout.boxes).filter(([id]) => id !== GRAPHBAN_BOX);
    for (const [id, b] of others) expect(b.x, id).toBeGreaterThan(box.x + box.w);
    expect(layout.hostFrames[0].id).toBe(BUILD_LANE);
    // Collapsed, the box names itself; the lane frame (and its collapse control) is
    // only for the expanded instance.
    expect(layout.lane).toBeNull();
    // The app's externals take the right-hand column; infra sits between.
    expect(layout.boxes["ext:stripe"].x).toBeGreaterThan(layout.boxes["infra:pg"].x);
    expect(layout.boxes["infra:pg"].x).toBeGreaterThan(layout.boxes["app:backend"].x);
  });

  it("sits infra beside the deployable that uses it, not above it", () => {
    const backend = layout.boxes["app:backend"];
    expect(layout.boxes["infra:pg"].y).toBeGreaterThanOrEqual(backend.y);
    expect(layout.boxes["infra:redis"].y).toBeGreaterThanOrEqual(backend.y);
  });

  it("keeps unresolved references as their own box in ring 3", () => {
    expect(ids).toContain("unresolved:FOO_URL");
    expect(view.topo.nodes.find((n) => n.id === "unresolved:FOO_URL")!.ring).toBe(3);
  });

  it("a failed probe reads the collapsed box unreachable, never reachable", () => {
    const t = republiqRecord();
    t.nodes = t.nodes.map((n) => (n.id === "db" ? { ...n, liveness: "unreachable" } : n));
    const box = prepareStackMap(t).topo.nodes.find((n) => n.id === GRAPHBAN_BOX)!;
    expect(box.liveness).toBe("unreachable");
  });

  it("expanding draws the instance in place, framed, with no extra box", () => {
    const open = prepareStackMap(republiqRecord(), true);
    const openIds = open.topo.nodes.map((n) => n.id);
    expect(openIds).not.toContain(GRAPHBAN_BOX);
    for (const gid of ["api", "web", "db", "cli", "openai"]) expect(openIds).toContain(gid);
    const l = buildStackLayout(open.topo, open.layoutOptions);
    expect(l.lane).not.toBeNull();
    // The instance stays in the lane, left of the app.
    expect(l.boxes["api"].x + l.boxes["api"].w).toBeLessThan(l.boxes["app:backend"].x);
    for (const id of ["api", "web", "db", "cli"]) {
      const b = l.boxes[id];
      expect(b.x >= l.lane!.x && b.x + b.w <= l.lane!.x + l.lane!.w, id).toBe(true);
    }
  });
});

describe("prepareStackMap — states before and after a record", () => {
  it("with no record, ring 3 reads 'services not mapped yet', never empty", () => {
    const t = republiqRecord();
    t.mapping = { ...t.mapping!, state: "unmapped", mapped_sha: null };
    t.nodes = t.nodes.filter((n) => !/^(ext|unresolved|infra):/.test(n.id));
    const view = prepareStackMap(t);
    expect(view.topo.nodes.map((n) => n.id)).toContain(UNMAPPED_BOX);
    const l = buildStackLayout(view.topo, view.layoutOptions);
    expect(l.rings.find((r) => r.ring === 3)!.subtitle).toBe(SERVICES_NOT_MAPPED);
    expect(l.boxes[UNMAPPED_BOX]).toBeDefined();
  });

  it("an older API with no mapping block is unmapped, not mapped", () => {
    const t = republiqRecord();
    delete t.mapping;
    expect(prepareStackMap(t).layoutOptions.ring3Subtitle).toBe(SERVICES_NOT_MAPPED);
  });

  it("a mapped record keeps ring 3's own gloss", () => {
    const view = prepareStackMap(republiqRecord());
    expect(view.layoutOptions.ring3Subtitle).toBeNull();
    expect(view.topo.nodes.map((n) => n.id)).not.toContain(UNMAPPED_BOX);
  });
});

describe("prepareStackMap — Graphban's own project", () => {
  const grph: StackTopology = { ...topo, graphban_source: "owned" };
  const view = prepareStackMap(grph);

  it("draws today's map with no self-box and nothing collapsible", () => {
    expect(view.collapsible).toBe(false);
    expect(view.topo.nodes).toEqual(grph.nodes);
    expect(view.topo.edges).toEqual(grph.edges);
    expect(view.layoutOptions.graphbanLeft).toBe(false);
    expect(buildStackLayout(view.topo, view.layoutOptions).lane).toBeNull();
  });

  it("an older API with ring-1 Graphban boxes reads as owned", () => {
    const { graphban_source: _, ...old } = grph;
    expect(prepareStackMap(old as StackTopology).collapsible).toBe(false);
  });
});

/**
 * Axis-aligned segment through a box interior (2px inside the border — the same
 * predicate the router uses). A diagonal is reported as a hit: the route is
 * required to be orthogonal, and a diagonal would otherwise slip past this check.
 */
function segmentHitsInterior(
  a: { x: number; y: number },
  b: { x: number; y: number },
  bx: { x: number; y: number; w: number; h: number },
): boolean {
  const x0 = bx.x + 2;
  const x1 = bx.x + bx.w - 2;
  const y0 = bx.y + 2;
  const y1 = bx.y + bx.h - 2;
  if (Math.abs(a.y - b.y) < 0.5) {
    return a.y > y0 && a.y < y1 && Math.max(a.x, b.x) > x0 && Math.min(a.x, b.x) < x1;
  }
  if (Math.abs(a.x - b.x) < 0.5) {
    return a.x > x0 && a.x < x1 && Math.max(a.y, b.y) > y0 && Math.min(a.y, b.y) < y1;
  }
  return true;
}

function assertRoutesMissOtherBoxes(layout: ReturnType<typeof buildStackLayout>) {
  for (const e of layout.edges) {
    expect(e.points.length).toBeGreaterThanOrEqual(2);
    for (let i = 0; i < e.points.length - 1; i++) {
      const a = e.points[i];
      const b = e.points[i + 1];
      expect(
        Math.abs(a.x - b.x) < 0.5 || Math.abs(a.y - b.y) < 0.5,
        `edge ${e.from}->${e.to} segment ${i} is not orthogonal`,
      ).toBe(true);
      for (const [id, bx] of Object.entries(layout.boxes)) {
        if (id === e.from || id === e.to) continue;
        expect(
          segmentHitsInterior(a, b, bx),
          `edge ${e.from}->${e.to} segment ${i} crosses ${id}`,
        ).toBe(false);
      }
    }
  }
}

/** Boxes strictly between two horizontally separated boxes. Empty means adjacent columns. */
function boxesBetweenColumns(
  a: { x: number; y: number; w: number; h: number },
  b: { x: number; y: number; w: number; h: number },
  all: { x: number; y: number; w: number; h: number }[],
) {
  const left = a.x + a.w <= b.x + 0.5 ? a : b.x + b.w <= a.x + 0.5 ? b : null;
  if (!left) return null;
  const right = left === a ? b : a;
  return all.filter(
    (bx) => bx !== a && bx !== b && bx.x + bx.w > left.x + left.w + 1 && bx.x < right.x - 1,
  );
}

describe("buildStackLayout — obstacle-aware routes miss every other box", () => {
  // cli and api are one column apart, with fleet filling the column between them
  // at the same height. The two-bend elbow's midpoint lands inside fleet.
  const cross: StackTopology = {
    ...topo,
    hosts: [
      { id: "user_machines", label: "User machines", nodes: ["cli"] },
      { id: "agent_hosts", label: "Agent hosts", nodes: ["fleet"] },
      { id: "compose", label: "Compose", nodes: ["api", "web"] },
    ],
    nodes: [
      node({ id: "cli", ring: 1, host: "user_machines" }),
      node({ id: "fleet", ring: 1, host: "agent_hosts" }),
      node({ id: "api", ring: 1, host: "compose", surfaces: ["REST", "MCP"] }),
      node({ id: "web", ring: 1, host: "compose" }),
    ],
    edges: [
      { from: "cli", to: "api", protocol: "HTTP/JWT", label: "HTTP/JWT" },
      { from: "cli", to: "web", protocol: "HTTP", label: "HTTP" },
      { from: "fleet", to: "api", protocol: "HTTP", label: "HTTP" },
      { from: "web", to: "api", protocol: "HTTP", label: "HTTP" },
    ],
  };
  const layout = buildStackLayout(cross);

  it("lays cli, fleet and api out as three columns, fleet between the other two", () => {
    const { cli, fleet, api } = layout.boxes;
    expect(cli.x + cli.w).toBeLessThan(fleet.x);
    expect(fleet.x + fleet.w).toBeLessThan(api.x);
    // Same band, so a straight elbow cannot thread the gap — it has to cross fleet.
    expect(cli.y).toBeLessThan(fleet.y + fleet.h);
    expect(fleet.y).toBeLessThan(cli.y + cli.h);
  });

  it("routes cli→api, and every other edge, clear of every box it does not end on", () => {
    expect(layout.edges.map((e) => `${e.from}->${e.to}`)).toContain("cli->api");
    assertRoutesMissOtherBoxes(layout);
    const cliApi = layout.edges.find((e) => e.from === "cli" && e.to === "api")!;
    // The protocol label sits on the route, so it must not land on the box the line avoids.
    expect(segmentHitsInterior(cliApi.mid, cliApi.mid, layout.boxes.fleet)).toBe(false);
  });

  it("keeps an adjacent-column edge on the two-bend elbow", () => {
    const fleetApi = layout.edges.find((e) => e.from === "fleet" && e.to === "api")!;
    const between = boxesBetweenColumns(layout.boxes.fleet, layout.boxes.api, Object.values(layout.boxes));
    expect(between).toEqual([]);
    // Four points is the elbow: exit, one bend, the other bend, entry.
    expect(fleetApi.points.length).toBeLessThanOrEqual(4);
  });

  it("is deterministic", () => {
    const again = buildStackLayout(cross);
    expect(again.edges.map((e) => e.points)).toEqual(layout.edges.map((e) => e.points));
  });
});

describe("buildStackLayout — generated topologies keep every edge off other boxes", () => {
  const cols = 4;
  const rows = 3;
  const hosts: StackTopology["hosts"] = [];
  const nodes: StackNode[] = [];
  const edges: StackTopology["edges"] = [];
  for (let c = 0; c < cols; c++) {
    const ids: string[] = [];
    for (let r = 0; r < rows; r++) {
      const id = `c${c}r${r}`;
      ids.push(id);
      nodes.push(node({ id, ring: 1, host: `h${c}` }));
    }
    hosts.push({ id: `h${c}`, label: `Column ${c}`, nodes: ids });
  }
  for (let a = 0; a < cols; a++) {
    for (let b = 0; b < cols; b++) {
      if (a === b) continue;
      for (let ra = 0; ra < rows; ra++) {
        for (let rb = 0; rb < rows; rb++) {
          edges.push({
            from: `c${a}r${ra}`,
            to: `c${b}r${rb}`,
            protocol: "http",
            label: "http",
          });
        }
      }
    }
  }
  // A box between two others in the same column — the vertical form of the same miss.
  for (let c = 0; c < cols; c++) {
    edges.push({ from: `c${c}r0`, to: `c${c}r2`, protocol: "http", label: "http" });
  }
  const generated: StackTopology = { ...topo, hosts, nodes, edges };
  const layout = buildStackLayout(generated);

  it("has several columns and several boxes in each, and an edge for every column pair", () => {
    const byHost = new Map<string, number>();
    for (const n of nodes) byHost.set(n.host, (byHost.get(n.host) ?? 0) + 1);
    expect(byHost.size).toBe(cols);
    for (const count of byHost.values()) expect(count).toBe(rows);
    const pairs = new Set<string>();
    for (const e of edges) {
      const ha = nodes.find((n) => n.id === e.from)!.host;
      const hb = nodes.find((n) => n.id === e.to)!.host;
      if (ha !== hb) pairs.add([ha, hb].sort().join("|"));
    }
    expect(pairs.size).toBe((cols * (cols - 1)) / 2);
  });

  it("lets no routed segment enter a box that is not its own endpoint", () => {
    expect(layout.edges.length).toBe(edges.length);
    assertRoutesMissOtherBoxes(layout);
  });

  it("keeps adjacent-column edges to at most the two-bend elbow", () => {
    const values = Object.values(layout.boxes);
    let seen = 0;
    for (const e of layout.edges) {
      const between = boxesBetweenColumns(layout.boxes[e.from], layout.boxes[e.to], values);
      if (between === null || between.length > 0) continue;
      seen += 1;
      expect(e.points.length, `${e.from}->${e.to}`).toBeLessThanOrEqual(4);
    }
    expect(seen).toBeGreaterThan(0);
  });

  it("is deterministic", () => {
    const again = buildStackLayout(generated);
    expect(again.edges.map((e) => e.points)).toEqual(layout.edges.map((e) => e.points));
  });
});
