import { describe, expect, it } from "vitest";

import { cursorWaveConfig, envBlock, ROLE_ENV, serverName, WAVE_ROLES } from "@/features/fleet/wave";

/**
 * The wave block is pasted into a shell and a config file, so a wrong one fails as
 * `unauthorized` — which reads as a bad key rather than a bad variable name, and costs an
 * afternoon to tell apart.
 *
 * The property that carries all of it: **three roles, three DIFFERENT credentials.** One key
 * across the servers is what Cursor already does, and it is precisely what makes every review
 * non-independent — an author and a reviewer sharing a credential and a host are not two
 * opinions. Collapsing them would leave the setup looking complete while silently restoring
 * the bug it exists to fix.
 */
describe("wave provisioning", () => {
  it("exports one variable per role and never repeats a name", () => {
    const block = envBlock({ planner: "gb_sk_p", worker: "gb_sk_w", reviewer: "gb_sk_r" });

    expect(block.split("\n")).toHaveLength(3);
    for (const role of WAVE_ROLES) expect(block).toContain(`export ${ROLE_ENV[role]}=`);
    expect(new Set(Object.values(ROLE_ENV)).size).toBe(WAVE_ROLES.length);
  });

  it("emits only the roles minted so far", () => {
    // Minting is sequential, and a failure part-way should still show the credentials that
    // now exist server-side — they cannot be re-shown, so dropping them would strand them.
    const block = envBlock({ planner: "gb_sk_p" });

    expect(block).toBe(`export ${ROLE_ENV.planner}=gb_sk_p`);
  });

  it("names a separate server per role, each reading its own variable", () => {
    const cfg = JSON.parse(cursorWaveConfig("https://gb.example/api/mcp"));

    expect(Object.keys(cfg.mcpServers)).toEqual(WAVE_ROLES.map(serverName));
    const headers = WAVE_ROLES.map((r) => cfg.mcpServers[serverName(r)].headers["X-API-Key"]);
    expect(new Set(headers).size).toBe(WAVE_ROLES.length);
    for (const role of WAVE_ROLES) {
      expect(cfg.mcpServers[serverName(role)].headers["X-API-Key"]).toBe(`\${env:${ROLE_ENV[role]}}`);
    }
  });

  it("the config never contains a credential, only a reference to one", () => {
    // This is the half that gets committed or shared. It is also what makes it write-once:
    // the per-wave part lives in the environment, so the file never changes again.
    const cfg = cursorWaveConfig("https://gb.example/api/mcp");

    expect(cfg).not.toMatch(/gb_sk_|al_sk_/);
    expect(cfg).toContain("${env:");
  });

  it("points every server at the host it was generated from", () => {
    const cfg = JSON.parse(cursorWaveConfig("https://gb.example/api/mcp"));

    for (const role of WAVE_ROLES) {
      expect(cfg.mcpServers[serverName(role)].url).toBe("https://gb.example/api/mcp");
    }
  });
});
