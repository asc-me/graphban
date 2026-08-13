import { describe, expect, it } from "vitest";

import { cursorWaveConfig, instanceHint, serverName, WAVE_ROLES } from "@/features/fleet/wave";

/**
 * The wave config is pasted into `~/.cursor/mcp.json`, so a wrong one fails as a server that
 * simply is not there — Cursor drops an entry it cannot make sense of rather than erroring.
 *
 * The property that carries all of it: **three roles, three DIFFERENT credentials.** One key
 * across the servers is what Cursor does by default, and it is exactly what makes every review
 * non-independent — an author and reviewer sharing a credential are not two opinions.
 * Collapsing them leaves the setup looking complete while silently restoring the bug.
 *
 * The keys are LITERAL. An earlier version referenced `${env:VAR}`; probed against Cursor
 * 3.16.2 with the variables present in the process environment, `${env:VAR}`, `${VAR}` and
 * `$VAR` were all ignored in both `url` and `headers`. Regenerating the file per wave is the
 * cost of that, and it is why this must never quietly go back to a reference.
 */
describe("wave provisioning", () => {
  const KEYS = { planner: "gb_sk_p", worker: "gb_sk_w", reviewer: "gb_sk_r" };

  it("names a separate server per role, each with its own credential", () => {
    const cfg = JSON.parse(cursorWaveConfig("https://gb.example/api/mcp", KEYS));

    expect(Object.keys(cfg.mcpServers)).toEqual(WAVE_ROLES.map(serverName));
    const headers = WAVE_ROLES.map((r) => cfg.mcpServers[serverName(r)].headers["X-API-Key"]);
    expect(new Set(headers).size).toBe(WAVE_ROLES.length);
  });

  it("writes the key literally, because a reference is silently dropped", () => {
    const cfg = JSON.parse(cursorWaveConfig("https://gb.example/api/mcp", KEYS));

    for (const role of WAVE_ROLES) {
      expect(cfg.mcpServers[serverName(role)].headers["X-API-Key"]).toBe(KEYS[role]);
    }
    // The regression that would look correct and connect nothing.
    expect(cursorWaveConfig("https://gb.example/api/mcp", KEYS)).not.toContain("${");
  });

  it("emits only the roles minted so far", () => {
    // Minting is sequential, and a failure part-way should still hand over credentials that
    // now exist server-side — they cannot be re-shown, so dropping them would strand them.
    const cfg = JSON.parse(cursorWaveConfig("https://gb.example/api/mcp", { planner: "gb_sk_p" }));

    expect(Object.keys(cfg.mcpServers)).toEqual(["graphban-planner"]);
  });

  it("points every server at the host it was generated from", () => {
    const cfg = JSON.parse(cursorWaveConfig("https://gb.example/api/mcp", KEYS));

    for (const role of WAVE_ROLES) {
      expect(cfg.mcpServers[serverName(role)].url).toBe("https://gb.example/api/mcp");
    }
  });

  it("the instance hint differs per agent", () => {
    // For the shared-credential fallback: on one key, two agents declaring the SAME instance
    // are not two opinions, and the server refuses the review. A hint that repeated would
    // reproduce exactly that.
    expect(instanceHint("worker", 1)).not.toBe(instanceHint("worker", 2));
    expect(instanceHint("worker", 1)).toContain("instance");
  });
});
