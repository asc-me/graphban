import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { ImportRunReport } from "@/lib/types";

import { ImportReport } from "./ImportReport";

vi.mock("@/lib/api", () => ({ api: { importRuns: vi.fn(), importRun: vi.fn(), adoptImportRun: vi.fn() } }));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));
vi.mock("./LibraryRuleControls", () => ({ useCanWriteRules: () => true }));

const report: ImportRunReport = {
  id: 3, project_id: "core", commit: "abc12345def", created_by: "alex", created_at: null,
  sources_read: [{ path: "CODEOWNERS" }],
  sources_unread: [{ path: "docs/adr/1.md", reason: "missing permission" }],
  findings: [
    { id: 10, bucket: "proposed", stage: "Plan", quote: "serialise migrations", source: { path: "CODEOWNERS", url: "https://x/CODEOWNERS" }, label: null, text: null, proposal_id: 7, guidance_id: null,
      proposal: { id: 7, status: "open" } as never },
    { id: 12, bucket: "proposed", stage: "Plan", quote: "uncited rule", source: { path: "CODEOWNERS" }, label: null, text: null, proposal_id: null, guidance_id: null, refused: "a proposal must cite its source", proposal: null },
    { id: 11, bucket: "external", stage: "Plan", quote: "CI required", source: { path: "CONTRIBUTING.md" }, label: null, text: null, proposal_id: null, guidance_id: null, proposal: null },
    { id: 13, bucket: "notice", stage: "Build", quote: "build.engine_evidence: no finding at c3 still supports it", source: { path: ".github/workflows/ci.yml" }, label: "source gone", text: "The rule is unchanged: only a person loosens or removes a rule.", proposal_id: null, guidance_id: null, proposal: null },
  ],
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.importRuns).mockResolvedValue([{ id: 3, commit: "abc", created_by: "alex", created_at: null, unread: 1 }]);
  vi.mocked(api.importRun).mockResolvedValue(report);
  vi.mocked(api.adoptImportRun).mockResolvedValue([]);
});

describe("ImportReport", () => {
  it("shows unread sources, groups by bucket, and adopts the ticked batch with one reason", async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ImportReport />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/docs\/adr\/1.md — missing permission/)).toBeInTheDocument();
    expect(screen.getByText("Proposed rules")).toBeInTheDocument();
    expect(screen.getByText("Enforced outside Graphban")).toBeInTheDocument();
    expect(screen.getByText(/refused: a proposal must cite its source/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Adopt finding 12")).not.toBeInTheDocument();
    // A rescan's drift notice is shown and cannot be ticked: nothing is filed from it.
    expect(screen.getByText("Drift (shown, never acted on)")).toBeInTheDocument();
    expect(screen.getByText("source gone:")).toBeInTheDocument();
    expect(screen.getByText(/only a person loosens or removes a rule/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Adopt finding 13")).not.toBeInTheDocument();
    const adopt = screen.getByRole("button", { name: /adopt/i });
    expect(adopt).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Adopt finding 10"));
    await userEvent.type(screen.getByLabelText("Reason for the batch"), "matches repo");
    await userEvent.click(screen.getByRole("button", { name: /adopt 1 selected/i }));
    await waitFor(() => expect(api.adoptImportRun).toHaveBeenCalledWith("core", 3, [10], "matches repo"));
  });
});
