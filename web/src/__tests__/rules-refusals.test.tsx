import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EventPanel } from "@/features/activity/EventPanel";
import { RefusalRuleLink } from "@/features/activity/RefusalRuleLink";
import { TargetActivity } from "@/features/activity/TargetActivity";
import { errorRuleId, isRefusal } from "@/lib/errors";
import { RulesView } from "@/features/rules/RulesView";
import { api } from "@/lib/api";
import type { Event, RulesFlow, UnmappedRefusals } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: { rulesFlow: vi.fn(), unmappedRefusals: vi.fn(), recommendedRules: vi.fn(), dismissRecommendedRule: vi.fn(), createItem: vi.fn(), refusalCoverage: vi.fn(), events: vi.fn() },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const flow: RulesFlow = {
  version: "1.0.0",
  model: "prs_to_base",
  control: { state: "local", writable: true, message: "", org_name: "" },
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
  vi.mocked(api.unmappedRefusals).mockResolvedValue({ days: 7, recording_since: null, groups: [] });
  vi.mocked(api.refusalCoverage).mockResolvedValue({ published: false, n: null, m: null, commit: null, published_at: null });
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

  it("?rule= puts keyboard focus on the rule row", async () => {
    renderAt("/rules?rule=review.not_own_work&from=7");
    const row = await screen.findByTestId("rules-row-review.not_own_work");
    await waitFor(() => expect(document.activeElement).toBe(row));
  });

  it("a rule the registry lacks is said, not silently ignored", async () => {
    renderAt("/rules?rule=ghost.rule&from=7");
    expect(await screen.findByTestId("rules-focus-missing")).toBeTruthy();
  });
});

describe("side sheets are dialogs", () => {
  it("opening a sheet closes the inspector, traps Tab, closes on Escape and returns focus to its opener", async () => {
    const user = userEvent.setup();
    renderAt("/rules?stage=review");
    await screen.findByTestId("rules-inspector");
    const opener = screen.getByTestId("rules-open-unmapped");
    expect(opener).toHaveAttribute("aria-haspopup", "dialog");
    expect(opener).toHaveAttribute("aria-expanded", "false");
    await user.click(opener);
    const sheet = await screen.findByTestId("rules-unmapped");
    expect(screen.queryByTestId("rules-inspector")).not.toBeInTheDocument();
    expect(sheet).toHaveAttribute("aria-modal", "true");
    expect(opener).toHaveAttribute("aria-expanded", "true");
    expect(sheet.contains(document.activeElement)).toBe(true);
    for (let i = 0; i < 12; i++) {
      await user.tab();
      expect(sheet.contains(document.activeElement)).toBe(true);
    }
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("rules-unmapped")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);
  });
});

function unmappedBody(over: Partial<UnmappedRefusals> = {}): UnmappedRefusals {
  return { days: 7, recording_since: "2026-10-01T00:00:00Z", groups: [], ...over };
}

describe("unmapped refusals", () => {
  it("shows the server's ranking with rank, last seen, and the count on the header button", async () => {
    vi.mocked(api.unmappedRefusals).mockResolvedValue(unmappedBody({
      groups: [
        { where: "B", status: 403, platform: false, count: 5, last_at: "2026-10-28T10:00:00Z", daily: [0, 0, 0, 0, 0, 2, 3] },
        { where: "A", status: 409, platform: false, count: 1, last_at: "2026-10-27T10:00:00Z", daily: [0, 0, 0, 0, 0, 1, 0] },
      ],
    }));
    renderAt("/rules");
    await waitFor(() => expect(screen.getByTestId("rules-unmapped-count").textContent).toContain("6"));
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    const rows = await screen.findAllByTestId("rules-unmapped-row");
    expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining("#1B"), expect.stringContaining("#2A")]);
    expect(screen.getAllByTestId("rules-unmapped-last")).toHaveLength(2);
    expect(screen.getByTestId("rules-unmapped").textContent).toContain("LAST 7 DAYS");
    expect(screen.getByTestId("rules-unmapped-footer").textContent).toMatch(/Mapping a refusal is a code change/);
  });

  it("a published build shows N of M and the commit", async () => {
    vi.mocked(api.refusalCoverage).mockResolvedValue({ published: true, n: 40, m: 50, commit: "abcdef1234", published_at: "2026-10-29T00:00:00+00:00" });
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    await waitFor(() => expect(screen.getByTestId("rules-coverage-n").textContent).toBe("40"));
    expect(screen.getByTestId("rules-coverage-m").textContent).toBe("50");
    expect(screen.getByTestId("rules-unmapped-coverage").textContent).toContain("abcdef1");
  });

  it("an unpublished build says so and shows no number", async () => {
    vi.mocked(api.refusalCoverage).mockResolvedValue({ published: false, n: null, m: null, commit: null, published_at: null });
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    const line = await screen.findByTestId("rules-unmapped-coverage");
    await waitFor(() => expect(line.textContent).toMatch(/Not published by this build/));
    expect(line.textContent).not.toMatch(/\d/);
  });

  it("empty says since when, and the coverage line is not published rather than zero", async () => {
    vi.mocked(api.refusalCoverage).mockResolvedValue({ published: false, n: null, m: null, commit: null, published_at: null });
    vi.mocked(api.unmappedRefusals).mockResolvedValue(unmappedBody());
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    expect((await screen.findByTestId("rules-unmapped-empty")).textContent).toMatch(/No unmapped refusals recorded since .*2026/);
    await waitFor(() => expect(screen.getByTestId("rules-unmapped-coverage").textContent).toMatch(/not published/i));
  });

  it("empty with nothing ever recorded does not read as clean", async () => {
    vi.mocked(api.unmappedRefusals).mockResolvedValue(unmappedBody({ recording_since: null }));
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    expect((await screen.findByTestId("rules-unmapped-empty")).textContent).toMatch(/not a clean result/);
  });

  it("an error says unknown, shows the failed request, and Retry calls the route again", async () => {
    vi.mocked(api.unmappedRefusals).mockRejectedValue(new Error("boom"));
    renderAt("/rules");
    await userEvent.click(screen.getByTestId("rules-open-unmapped"));
    expect((await screen.findByTestId("rules-unmapped-error")).textContent).toMatch(/unknown, not zero/);
    expect(screen.getByTestId("rules-unmapped-request").textContent).toContain("/api/rules/refusals/unmapped?project_id=core");
    const before = vi.mocked(api.unmappedRefusals).mock.calls.length;
    vi.mocked(api.unmappedRefusals).mockResolvedValue(unmappedBody());
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(vi.mocked(api.unmappedRefusals).mock.calls.length).toBeGreaterThan(before));
    expect(await screen.findByTestId("rules-unmapped-empty")).toBeTruthy();
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

describe("refusals where the work is (GRPH-1199)", () => {
  it("a PRD's own activity shows its refusal with a link focused on the rule", async () => {
    vi.mocked(api.events).mockResolvedValue({
      results: [ev({ id: 9, target_type: "prd", target_id: "GRPH-P61", meta: { rule_id: "review.not_own_work" } })],
      total: 1, limit: 50, offset: 0, has_more: false,
    } as never);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><TargetActivity projectId="core" targetType="prd" targetId="GRPH-P61" /></MemoryRouter>
      </QueryClientProvider>,
    );
    const link = await screen.findByRole("link", { name: "see rule" });
    expect(link.getAttribute("href")).toBe("/rules?rule=review.not_own_work&from=9");
    expect(vi.mocked(api.events).mock.calls.at(-1)?.[0]).toMatchObject({ targetType: "prd", targetId: "GRPH-P61" });
  });

  it("an inline refusal with a rule_id gets 'see rule'; one without says unmapped", () => {
    const mapped = new Error(JSON.stringify({ detail: "no", rule_id: "review.not_own_work" }));
    const unmapped = new Error(JSON.stringify({ detail: "no", rule_id: null }));
    expect(isRefusal(mapped)).toBe(true);
    expect(errorRuleId(mapped)).toBe("review.not_own_work");
    expect(errorRuleId(unmapped)).toBeNull();
    render(<MemoryRouter><RefusalRuleLink ruleId={errorRuleId(mapped)} /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "see rule" }).getAttribute("href")).toBe("/rules?rule=review.not_own_work");
    render(<MemoryRouter><RefusalRuleLink ruleId={errorRuleId(unmapped)} /></MemoryRouter>);
    expect(screen.getByText(/isn't mapped to a rule yet/)).toBeTruthy();
  });

  it("changing ?rule= while mounted refocuses", async () => {
    const two = structuredClone(flow);
    two.stages[0].rules.push({ ...two.stages[0].rules[0], id: "review.other", title: "Other" });
    vi.mocked(api.rulesFlow).mockResolvedValue(two);
    let go: (to: string) => void = () => {};
    function Nav() { const n = useNavigate(); go = n; return null; }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/rules?rule=review.not_own_work"]}><Nav /><RulesView /></MemoryRouter>
      </QueryClientProvider>,
    );
    expect((await screen.findByTestId("rules-row-review.not_own_work")).getAttribute("data-focused")).toBe("true");
    React.act(() => go("/rules?rule=review.other"));
    await waitFor(() => expect(screen.getByTestId("rules-row-review.other").getAttribute("data-focused")).toBe("true"));
  });
});
