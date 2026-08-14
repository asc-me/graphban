import { describe, expect, it } from "vitest";

import {
  cloudsFor,
  formatRemaining,
  holdersOf,
  indexByNode,
  roleHex,
  secondsRemaining,
} from "@/lib/graph/presence";
import type { FleetPresence, HeldArea } from "@/lib/types";

const SERVED = "2026-08-14T12:00:00.000Z";

function held(over: Partial<HeldArea> = {}): HeldArea {
  return {
    area: "a.py",
    item_id: "GRPH-1",
    expires_at: "2026-08-14T12:10:00.000Z",
    agent_id: "GRPH-A1",
    agent_label: "claude @ mbp",
    active_role: "worker",
    state: "working",
    user_id: "u1",
    user_initials: "AC",
    user_color: "#a78bfa",
    node_paths: ["a.py"],
    predicted: false,
    ...over,
  };
}

const presence = (rows: HeldArea[], off: HeldArea[] = []): FleetPresence => ({
  served_at: SERVED,
  heartbeat_interval_seconds: 50,
  held: rows,
  off_map: off,
  truncated: false,
  total: rows.length + off.length,
});

describe("indexByNode", () => {
  it("maps every node an area resolved to, not just the first", () => {
    const idx = indexByNode(presence([held({ node_paths: ["a.py", "b.py"] })]));
    expect([...idx.keys()].sort()).toEqual(["a.py", "b.py"]);
  });

  it("collects two holders of the same node in a stable order", () => {
    const idx = indexByNode(presence([
      held({ agent_id: "GRPH-A9", user_id: "u2" }),
      held({ agent_id: "GRPH-A2", user_id: "u1" }),
    ]));
    expect(idx.get("a.py")!.map((r) => r.agent_id)).toEqual(["GRPH-A2", "GRPH-A9"]);
  });

  it("is empty for undefined presence rather than throwing", () => {
    expect(indexByNode(undefined).size).toBe(0);
  });

  it("ignores off_map rows — they have no node to index against", () => {
    const idx = indexByNode(presence([], [held({ node_paths: undefined, reason: "undescribed" })]));
    expect(idx.size).toBe(0);
  });
});

describe("holdersOf", () => {
  it("groups by human, not by agent — two windows are one person", () => {
    const rows = [
      held({ agent_id: "GRPH-A1", user_id: "u1", node_paths: ["a.py"] }),
      held({ agent_id: "GRPH-A2", user_id: "u1", node_paths: ["b.py"] }),
    ];
    const holders = holdersOf(presence(rows));
    expect(holders).toHaveLength(1);
    expect(holders[0].agents.size).toBe(2);
    expect(holders[0].nodes.size).toBe(2);
  });

  it("keeps distinct users apart and orders them stably", () => {
    const holders = holdersOf(presence([
      held({ user_id: "u2", user_color: "#ff0000" }),
      held({ user_id: "u1" }),
    ]));
    expect(holders.map((h) => h.userId)).toEqual(["u1", "u2"]);
  });

  it("falls back to a muted colour when the user has none", () => {
    expect(holdersOf(presence([held({ user_color: null })]))[0].color).toBe("#8b949e");
  });
});

describe("cloudsFor", () => {
  const pos = {
    "a.py": { x: 100, y: 100 },
    "b.py": { x: 140, y: 120 },
    "far.py": { x: 800, y: 500 },
  };

  it("merges nearby held nodes of one user into a single blob", () => {
    const clouds = cloudsFor(presence([held({ node_paths: ["a.py", "b.py"] })]), pos);
    expect(clouds).toHaveLength(1);
    expect(clouds[0].count).toBe(2);
    // A fleet working an area should read as one region, not a scatter of dots.
    expect(clouds[0].cx).toBeCloseTo(120, 6);
  });

  it("splits a user's distant nodes into separate blobs", () => {
    const clouds = cloudsFor(presence([held({ node_paths: ["a.py", "far.py"] })]), pos);
    expect(clouds).toHaveLength(2);
    expect(clouds.map((c) => c.count).sort()).toEqual([1, 1]);
  });

  it("gives each user their own blob over the same node", () => {
    const clouds = cloudsFor(presence([
      held({ user_id: "u1", user_color: "#aaa" }),
      held({ user_id: "u2", user_color: "#bbb" }),
    ]), pos);
    expect(clouds).toHaveLength(2);
    expect(clouds.map((c) => c.color).sort()).toEqual(["#aaa", "#bbb"]);
  });

  it("scales the radius with the spread it covers", () => {
    const tight = cloudsFor(presence([held({ node_paths: ["a.py", "b.py"] })]), pos)[0];
    const single = cloudsFor(presence([held({ node_paths: ["a.py"] })]), pos)[0];
    expect(tight.r).toBeGreaterThan(single.r);
  });

  it("marks a blob predicted when any area feeding it was a guess", () => {
    const clouds = cloudsFor(presence([held({ predicted: true })]), pos);
    expect(clouds[0].predicted).toBe(true);
    expect(cloudsFor(presence([held()]), pos)[0].predicted).toBe(false);
  });

  it("skips nodes the layout has not placed instead of emitting NaN", () => {
    const clouds = cloudsFor(presence([held({ node_paths: ["ghost.py"] })]), pos);
    expect(clouds).toEqual([]);
  });

  it("is deterministic across identical inputs", () => {
    const rows = [held({ node_paths: ["a.py", "b.py"] }), held({ user_id: "u2", node_paths: ["far.py"] })];
    expect(cloudsFor(presence(rows), pos)).toEqual(cloudsFor(presence(rows), pos));
  });
});

describe("secondsRemaining", () => {
  it("measures against the payload's served_at, not the browser clock", () => {
    // A viewer whose clock is a minute out must not see every lease as a minute short.
    expect(secondsRemaining("2026-08-14T12:10:00.000Z", SERVED)).toBe(600);
  });

  it("clamps an expired lease to zero rather than counting backwards", () => {
    expect(secondsRemaining("2026-08-14T11:59:00.000Z", SERVED)).toBe(0);
  });

  it("returns null when there is no expiry to measure", () => {
    expect(secondsRemaining(null, SERVED)).toBeNull();
    expect(secondsRemaining("not-a-date", SERVED)).toBeNull();
  });
});

describe("formatRemaining", () => {
  it("renders the shapes an inspector line needs", () => {
    expect(formatRemaining(48)).toBe("48s");
    expect(formatRemaining(250)).toBe("4m 10s");
    expect(formatRemaining(120)).toBe("2m");
    expect(formatRemaining(0)).toBe("expired");
    expect(formatRemaining(null)).toBe("");
  });
});

describe("roleHex", () => {
  it("gives all-in-one a neutral colour, not one of the three roles", () => {
    // FleetView's reasoning, mirrored: an all-in-one agent is not a worker that also reviews,
    // it is the other posture, and tinting it as a role would say the opposite.
    expect(roleHex("all-in-one")).toBe("#8b949e");
    expect(roleHex("all-in-one")).not.toBe(roleHex("worker"));
  });

  it("falls back to muted for an unknown or missing role", () => {
    expect(roleHex(null)).toBe("#8b949e");
    expect(roleHex("nonsense")).toBe("#8b949e");
  });
});
