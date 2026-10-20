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
