import { describe, expect, it } from "vitest";

/**
 * PRD-21 acceptance criterion 17 — self-host loses nothing, and gains no org plane.
 *
 * G9: every addition is org-gated and absent when `hosted_mode` is off. D1.2 is specific
 * about the mechanism — the routes are **never registered**, not registered-and-guarded —
 * so a stale bookmark falls through the SPA's existing catch-all like any unknown path.
 * No 404 page, no minimal org UI, no special case.
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

  it("registers every org route inside a hosted gate", () => {
    // Every `path={ORG_BASE...}` and `path={adminPath...}` must sit after a `hosted &&`
    // in the same JSX block. The block is found by locating the gate and taking what
    // follows it up to the matching close, which is how the file is actually structured.
    const gate = app.indexOf("{hosted && (");
    expect(gate, "the hosted gate must exist").toBeGreaterThan(0);

    const before = app.slice(0, gate);
    expect(before, "no org route may be registered before the hosted gate").not.toMatch(
      /path=\{ORG_BASE/,
    );
  });

  it("mounts project views at the root when not hosted, and under /p/:tag when hosted", () => {
    expect(app).toMatch(/hosted\s*\n?\s*\?[\s\S]{0,200}\/p\/:tag\//);
    expect(app).toMatch(/PROJECT_VIEWS/);
    expect(app).toMatch(/path=\{`\/\$\{path\}`\}/);
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
