import { describe, expect, it } from "vitest";

/**
 * PRD-21 acceptance criterion 17 — self-host loses nothing, and gains no org plane.
 *
 * The org plane and the operator console render in graphban-control. This app does not
 * mount those screens. A hosted bookmark under the org base leaves for the console; a
 * self-host build still does not register that base, so a stale `/org` falls through
 * the catch-all like any unknown path.
 *
 * Asserted against App.tsx's source rather than a render, because what is under test is
 * the *registration*, and a render can only show what a given build registered. The
 * source says whether the gate exists at all.
 */
const SOURCES = import.meta.glob("../App.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const app = Object.values(SOURCES)[0];

describe("the org plane is absent on self-host, not guarded", () => {
  it("has App.tsx to read, or the assertions below mean nothing", () => {
    expect(app, "App.tsx must be readable").toBeTruthy();
    expect(app).toContain("ORG_BASE");
  });

  it("does not mount the org or operator screens", () => {
    expect(app).not.toContain("OrgOverviewView");
    expect(app).not.toContain("OrgAdminShell");
    expect(app).not.toContain("GalaxyView");
    expect(app).not.toContain("AdminView");
    expect(app).toContain("<ConsoleRedirect />");
  });

  it("sends hosted org bookmarks to the console, and does not register them on self-host", () => {
    // The redirect sits inside the hosted gate. A self-host build has no org to send
    // anywhere, so the base must not be registered before that gate.
    const gate = app.indexOf("{hosted && (");
    expect(gate, "the hosted gate must exist").toBeGreaterThan(0);

    const before = app.slice(0, gate);
    expect(before, "no org route may be registered before the hosted gate").not.toMatch(
      /path=\{ORG_BASE/,
    );
    const hosted = app.slice(gate);
    expect(hosted).toMatch(/path=\{ORG_BASE\} element=\{<ConsoleRedirect \/>\}/);
  });

  it("mounts project views at the root when not hosted, and under /p/:tag when hosted", () => {
    expect(app).toMatch(/hosted\s*\n?\s*\?[\s\S]{0,200}\/p\/:tag\//);
    expect(app).toMatch(/PROJECT_VIEWS/);
    expect(app).toMatch(/path=\{`\/\$\{path\}`\}/);
  });

  it("names Fleet as one page, and redirects fleet.v1 / fleet.v2 to its tabs", () => {
    expect(app).toContain('["fleet"');
    expect(app).not.toContain('["fleet.v');
    expect(app).toContain("FleetTabRedirect");
    expect(app).toContain('tab="harness"');
  });

  it("names Memory as one page, and redirects memory-review / memory-triage to it (hosted and flat)", () => {
    expect(app).toContain('["memory"');
    expect(app).not.toContain('["memory-review"');
    expect(app).not.toContain("MemoryTriageView");
    for (const old of ["memory-review", "memory-triage"]) {
      expect(app).toContain(`path="/${old}" element={<MemoryRedirect />}`);
      expect(app).toContain(`path="/p/:tag/${old}" element={<MemoryRedirect />}`);
      expect(app).toContain(`path="/${old}" element={<FlatRedirect to="memory" />}`);
    }
  });

  it("clears org state on boot for a self-host build", () => {
    // D1.2: a self-host build must not resurrect an org context it has no way to serve.
    const routes = import.meta.glob("../lib/routes.ts", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    expect(Object.values(routes)[0]).toContain("clearOrgStateForSelfHost");

    // Called from the shell rather than the router — the clear has to happen once config
    // is known, and AppFrame is where config lands. Asserting it in App.tsx was wrong
    // about the location, not about the behaviour.
    const frame = import.meta.glob("../components/shell/AppFrame.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(frame)[0];
    expect(src).toContain("clearOrgStateForSelfHost");
    expect(src, "it must be conditional on NOT hosted, or it would clear a live org").toMatch(
      /!hosted\)?\s*clearOrgStateForSelfHost/,
    );
  });
});
