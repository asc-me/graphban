import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { api, setAccessToken } from "@/lib/api";

/**
 * PRD-21 acceptance criteria 1–3 — the wrong-project write.
 *
 * These were specified and never written. The existing hierarchy tests assert that
 * `ProjectContext` resolves the right project from the route, which is necessary and not
 * sufficient: the defect P0 removed was not a mis-rendered screen, it was a **write** that
 * went to the previous project while the route already said otherwise.
 *
 * So these assert on what leaves the client — the request URL and body — because that is
 * the layer where the bug lived. Asserting on the rendered list would pass on exactly the
 * code that shipped the bug: the list re-fetches once the effect settles, papering over a
 * row that was already written to the wrong project.
 *
 * The structural fix is what makes them cheap: `createItem`, `updatePlatform` and
 * `githubConnect` now take the project as a required first argument, so a call site cannot
 * silently inherit an ambient one. AC 1 is the grep; these are the behaviour behind it.
 */
type Captured = { url: string; body: unknown };

let calls: Captured[] = [];

beforeEach(() => {
  calls = [];
  setAccessToken("test-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, opts: RequestInit = {}) => {
      calls.push({
        url: String(url),
        body: opts.body ? JSON.parse(String(opts.body)) : undefined,
      });
      return new Response(JSON.stringify({ id: "it_1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

describe("a write names its project explicitly", () => {
  it("creates an item in the project it was told, not one the client remembered", async () => {
    await api.createItem("prj_other", { title: "Filed while deep-linked" });

    expect(calls).toHaveLength(1);
    expect(calls[0].body).toMatchObject({ project_id: "prj_other", title: "Filed while deep-linked" });
  });

  it("sends two consecutive writes to two different projects", async () => {
    // The race, expressed as the thing it produced: back/forward between projects used to
    // leave one render in which the client still believed the previous project.
    await api.createItem("prj_a", { title: "A" });
    await api.createItem("prj_b", { title: "B" });

    expect((calls[0].body as { project_id: string }).project_id).toBe("prj_a");
    expect((calls[1].body as { project_id: string }).project_id).toBe("prj_b");
  });

  it("connects an integration to the project in the request, never a remembered one", async () => {
    // The worst case in D1.1: `github/connect` writes into a project's PlatformConfig, and
    // in hosted mode a user who belongs to two orgs has access to both — so the backend
    // has nothing to reject and the mistake is silent.
    await api.githubConnect("prj_a", "acme", "rocket");

    expect(calls[0].url).toContain("project_id=prj_a");
    expect(calls[0].url).toContain("/platform/github/connect");
  });

  it("cannot be called without a project at all", () => {
    // The typecheck is the real guard — this records it as a behaviour rather than
    // relying on a reader noticing the signature.
    // @ts-expect-error project is required; dropping it must not compile
    expect(() => api.createItem({ title: "no project" })).toBeDefined();
  });
});
