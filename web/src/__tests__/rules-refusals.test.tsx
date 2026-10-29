import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EventPanel } from "@/features/activity/EventPanel";
import { groupUnmapped } from "@/features/rules/RefusalPanels";
import { RulesView } from "@/features/rules/RulesView";
import { api } from "@/lib/api";
import type { Event, RulesFlow } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: { rulesFlow: vi.fn(), events: vi.fn(), recommendedRules: vi.fn(), dismissRecommendedRule: vi.fn(), createItem: vi.fn(), refusalCoverage: vi.fn() },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const flow: RulesFlow = {
  version: "1.0.0",
  model: "prs_to_base",
  edges: [],
  stages: [
    {
      id: "review", title: "Review", summary: "s", state: "enforced",
      rules: [{
        id: "review.not_own_work", stage: "review", title: "Own work", summary: "x", enforcement: "enforced", channel: null,
        enforcer: { kind: "graphban_source", ref: "a.py", url: "" }, truth: "code", scope: "project", models: [],
        value: null, source: null, used: true, label: null, skills: null,
      }],
    },
  ],
};

function ev(over: Partial<Event>): Event {
  return {
    id: 7, ts: new Date().toISOString(), actor_type: "user", actor_id: "u", actor_label: "u", surface: "rest",
    action: "refusal", target_type: "", target_id: "", project_id: "core", meta: null, ...over,
  };
}

function renderAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}><RulesView /></MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.rulesFlow).mockResolvedValue(flow);
});

describe("refusal links", () => {
  it("a mapped refusal links to its rule", () => {
    render(<MemoryRouter><EventPanel event={ev({ meta: { rule_id: "review.not_own_work" } })} refusals={["refusal"]} onClose={() => {}} onPivotTarget={() => {}} onPivotActor={() => {}} /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "see rule" }).getAttribute("href")).toBe("/rules?rule=review.not_own_work&from=7");
  });

  it("an unmapped refusal says so and links to the unfocused map", () => {
    render(<MemoryRouter><EventPanel event={ev({ action: "refusal_unmapped", meta: { where: "PATCH /api/x" } })} refusals={["refusal_unmapped"]} onClose={() => {}} onPivotTarget={() => {}} onPivotActor={() => {}} /></MemoryRouter>);
    expect(screen.getByText(/isn't mapped to a rule yet/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "see all rules" }).getAttribute("href")).toBe("/rules?pulse=unmapped");
  });

  it("?rule= focuses the rule and shows the breadcrumb back", async () => {
    renderAt("/rules?rule=review.not_own_work&from=7");
    const row = await screen.findByTestId("rules-row-review.not_own_work");
    expect(row.getAttribute("data-focused")).toBe("true");
    expect(screen.getByTestId("rules-breadcrumb").textContent).toContain("event 7");
  });

  it("a rule the registry lacks is said, not silently ignored", async () => {
    renderAt("/rules?rule=ghost.rule&from=7");
    expect(await screen.findByTestId("rules-focus-missing")).toBeTruthy();
  });
});

describe("unmapped refusals", () => {
  it("groups by site and status, ranked by count", () => {
    const g = groupUnmapped([
      ev({ meta: { where: "A", status: 409 } }), ev({ id: 8, meta: { where: "B", status: 403 } }), ev({ id: 9, meta: { where: "B", status: 403 } }),
    ]);
    expect(g.map((x) => [x.where, x.count])).toEqual([["B", 2], ["A", 1]]);
  });

  it("a published build shows N of M and the commit", async () => {
    vi.mocked(api.events).mockResolvedValue({ results: [], total: 0, limit: 500, offset: 0, has_more: false } as never);
    vi.mocked(api.refusalCoverage).mockResolvedValue({ published: true, n: 40, m: 50, commit: "abcdef1234", published_at: "2026-10-29T00:00:00+00:00" });
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    await waitFor(() => expect(screen.getByTestId("rules-coverage-n").textContent).toBe("40"));
    expect(screen.getByTestId("rules-coverage-m").textContent).toBe("50");
    expect(screen.getByTestId("rules-unmapped-coverage").textContent).toContain("abcdef1");
  });

  it("an unpublished build says so and shows no number", async () => {
    vi.mocked(api.events).mockResolvedValue({ results: [{ id: 1 }], total: 1, limit: 500, offset: 0, has_more: false } as never);
    vi.mocked(api.refusalCoverage).mockResolvedValue({ published: false, n: null, m: null, commit: null, published_at: null });
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    const line = await screen.findByTestId("rules-unmapped-coverage");
    await waitFor(() => expect(line.textContent).toMatch(/Not published by this build/));
    expect(line.textContent).not.toMatch(/\d/);
  });

  it("empty says so, and the coverage line is unknown rather than zero", async () => {
    vi.mocked(api.refusalCoverage).mockResolvedValue({ published: false, n: null, m: null, commit: null, published_at: null });
    vi.mocked(api.events).mockResolvedValue({ results: [], total: 0, limit: 500, offset: 0, has_more: false } as never);
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    expect(await screen.findByTestId("rules-unmapped-empty")).toBeTruthy();
    expect(screen.getByTestId("rules-unmapped-coverage").textContent).toMatch(/not published/i);
  });

  it("an error says unknown, never zero", async () => {
    vi.mocked(api.events).mockRejectedValue(new Error("boom"));
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    expect((await screen.findByTestId("rules-unmapped-error")).textContent).toMatch(/unknown, not zero/);
  });
});

describe("recommended rules", () => {
  it("lists sources, gates GitHub, and words chips without claiming enforcement", async () => {
    vi.mocked(api.recommendedRules).mockResolvedValue({
      sources: [
        { source: "stack", state: "read", read_at: null, reason: null },
        { source: "github", state: "not_available", read_at: null, reason: "GitHub is not connected" },
      ],
      suggestions: [
        { id: "a", stage: "release", level: "advisory", title: "Name the release doc", why: "w", kind: "value", rule_id: "release.release_defined_in", value: "RELEASING.md", evidence: [], fingerprint: "f" },
        { id: "b", stage: "merge", level: "enforced", title: "Gate X", why: "w", kind: "code", rule_id: null, value: null, evidence: [], fingerprint: "g" },
      ],
    });
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-recommended"));
    await waitFor(() => expect(screen.getByTestId("rules-source-github").textContent).toMatch(/NOT CONNECTED/));
    expect(screen.getByText(/Connect GitHub/)).toBeTruthy();
    expect(screen.getByTestId("rules-github-absent")).toBeTruthy();
    const chips = screen.getAllByTestId("rules-suggestion-chip").map((c) => c.textContent);
    expect(chips).toEqual(["WOULD BE ADVISORY", "ENFORCED ONCE BUILT"]);
    expect(screen.getByText("Needs a code change. Until it ships, nothing holds it.")).toBeTruthy();
  });

  it("a failed read is not 'nothing to suggest'", async () => {
    vi.mocked(api.recommendedRules).mockRejectedValue(new Error("down"));
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-recommended"));
    expect((await screen.findByTestId("rules-recommended-error")).textContent).toMatch(/not “nothing to suggest”/);
  });
});
