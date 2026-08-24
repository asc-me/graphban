import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, setAccessToken } from "@/lib/api";

/**
 * What the hubs panel ASKS FOR — PRD-20 AC-18, GRPH-480.
 *
 * The panel's own tests take `hubs` as a prop, so they cannot see this: they would pass
 * unchanged if the request ranked over every edge in the graph while the canvas beside it drew
 * a filtered subset. The decision was that the panel follows the edge-type chips, and the only
 * place that decision is observable is the URL, so this asserts there — the same reasoning as
 * `wrong-project-write.test.tsx`.
 */
let urls: string[] = [];

beforeEach(() => {
  urls = [];
  setAccessToken("test-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(String(url));
      return new Response(JSON.stringify({ hubs: [], components: [], path: null }), {
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

const q = (url: string) => new URLSearchParams(url.slice(url.indexOf("?") + 1));

describe("the ranking is scoped to the edges actually drawn", () => {
  it("sends only the edge types whose chips are on", async () => {
    await api.codeAnalysis({ projectId: "agentledger", edgeTypes: ["calls", "owns"] });

    expect(q(urls[0]).get("edge_types")).toBe("calls,owns");
  });

  it("never sends an empty edge_types, which would ask for nothing and read as everything", async () => {
    await api.codeAnalysis({ projectId: "agentledger", edgeTypes: [] });

    // Absence must mean "unscoped", and the only way to say that is to omit the parameter.
    expect(q(urls[0]).has("edge_types")).toBe(false);
  });

  it("carries the project, so the panel cannot rank a project it is not showing", async () => {
    await api.codeAnalysis({ projectId: "fleet-walk", edgeTypes: ["imports"] });

    expect(q(urls[0]).get("project_id")).toBe("fleet-walk");
  });

  it("asks for a path when given two endpoints (AC-19)", async () => {
    await api.codeAnalysis({
      projectId: "agentledger",
      a: "backend/app/mcp_server.py",
      b: "web/src/lib/queries.ts",
    });

    expect(q(urls[0]).get("a")).toBe("backend/app/mcp_server.py");
    expect(q(urls[0]).get("b")).toBe("web/src/lib/queries.ts");
  });

  it("does not ask for a path when it has no endpoints", async () => {
    await api.codeAnalysis({ projectId: "agentledger" });

    expect(q(urls[0]).has("a")).toBe(false);
    expect(q(urls[0]).has("b")).toBe(false);
  });
});
