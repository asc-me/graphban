/** GRPH-1583: a list param is edited as a list and written as a JSON list of strings. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LibraryRuleControls } from "@/features/rules/LibraryRuleControls";
import { api } from "@/lib/api";
import type { RuleFlowRule, RuleToggles } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: { gitops: vi.fn(), config: vi.fn(), syncStatus: vi.fn(), events: vi.fn(), ruleToggles: vi.fn(), setRuleToggle: vi.fn() },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

function rule(id: string, params: Record<string, string[]>): RuleFlowRule {
  return {
    id, stage: "build", title: "Generated files", summary: "s", enforcement: "checked", channel: null,
    enforcer: { kind: "doc", ref: "docs/rules.md", url: "" }, truth: "db", scope: "project",
    models: [], control: "toggle", value: true, source: "project",
    params: { value: params, source: "project" }, locked: false, used: true, label: null, skills: null,
  } as unknown as RuleFlowRule;
}

function show(r: RuleFlowRule) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <LibraryRuleControls rule={r} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.gitops).mockResolvedValue({ fields: {}, control: { state: "local", writable: true, message: "" }, projects: [] } as never);
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: false, org: "" } as never);
  vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [] } satisfies RuleToggles);
  vi.mocked(api.events).mockResolvedValue({ results: [], total: 0 } as never);
});

describe("list params", () => {
  it("says how to change a param and that a reason is needed", () => {
    show(rule("build.generated_files", { paths: [] }));
    expect(screen.getByTestId("rules-toggle-params-hint")).toHaveTextContent(/reason/);
  });

  it("writes an added pair of paths as a JSON list of strings", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggle).mockResolvedValue({} as never);
    show(rule("build.generated_files", { paths: [] }));
    const list = screen.getByRole("group", { name: "paths" });
    await user.click(within(list).getByRole("button", { name: "Add paths entry" }));
    await user.type(within(list).getByLabelText("paths entry 1"), "a/gen.ts");
    await user.click(within(list).getByRole("button", { name: "Add paths entry" }));
    await user.type(within(list).getByLabelText("paths entry 2"), "b/gen.ts");
    await user.click(screen.getByRole("button", { name: "Change params" }));
    await user.type(screen.getByLabelText("Reason"), "generated");
    await user.click(screen.getByRole("button", { name: "Change params" }));
    await waitFor(() =>
      expect(api.setRuleToggle).toHaveBeenCalledWith("core", "build.generated_files", {
        params: { paths: ["a/gen.ts", "b/gen.ts"] },
        reason: "generated",
      }),
    );
  });

  it("removes an entry and keeps an emptied list as []", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggle).mockResolvedValue({} as never);
    show(rule("merge.done_predicates", { predicates: ["suite_green"] }));
    await user.click(screen.getByRole("button", { name: "Remove predicates entry 1" }));
    await user.click(screen.getByRole("button", { name: "Change params" }));
    await user.type(screen.getByLabelText("Reason"), "x");
    await user.click(screen.getByRole("button", { name: "Change params" }));
    await waitFor(() =>
      expect(api.setRuleToggle).toHaveBeenCalledWith("core", "merge.done_predicates", { params: { predicates: [] }, reason: "x" }),
    );
  });

  it("shows the server's 422 text on the row", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggle).mockRejectedValue(new Error("done_predicates must include suite_green"));
    show(rule("merge.done_predicates", { predicates: ["suite_green", "x"] }));
    await user.click(screen.getByRole("button", { name: "Remove predicates entry 1" }));
    await user.click(screen.getByRole("button", { name: "Change params" }));
    await user.type(screen.getByLabelText("Reason"), "x");
    await user.click(screen.getByRole("button", { name: "Change params" }));
    expect(await screen.findByTestId("rules-toggle-error")).toHaveTextContent("suite_green");
  });
});
