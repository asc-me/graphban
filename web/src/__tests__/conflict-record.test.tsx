import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConflictRecordView } from "@/features/prds/ConflictRecordView";

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ active: { id: "core", name: "Core" }, activeId: "core", projects: [{ id: "core", name: "Core" }], setActiveId: () => {} }),
}));

const q = vi.hoisted(() => ({
  prd: { data: undefined as unknown },
  diff: { data: undefined as unknown },
}));
vi.mock("@/lib/queries", () => ({
  usePrd: () => q.prd,
  useIntentDiff: () => q.diff,
}));

const c = { prd_id: "P1", status: "approved", ledger_hash: "aaa", file_hash: "bbb", file_name: "p1.md", byte_length: 42, path: "prds/p1.md" };
const report = (over: Record<string, unknown> = {}) => ({
  folder: "/d", prds_dir: "/d/prds", exported: [], imported: [], updated_db: [], updated_file: [],
  conflicts: [], conflict_details: [], export_status: "ok", in_sync: 0, ...over,
});

function at(state: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/prds/conflict/P1", state }]}>
      <Routes><Route path="/prds/conflict/:id" element={<ConflictRecordView />} /></Routes>
    </MemoryRouter>,
  );
}

const current = () => screen.getAllByRole("listitem").filter((li) => li.getAttribute("aria-current") === "true").map((li) => li.textContent);

describe("ConflictRecordView", () => {
  beforeEach(() => {
    q.prd = { data: undefined };
    q.diff = { data: undefined };
  });

  it("shows both hashes and file facts, with no take-file or take-ledger", () => {
    at({ report: report({ conflicts: ["P1"], conflict_details: [c] }) });
    for (const t of ["aaa", "bbb", "p1.md", "42", "prds/p1.md"]) expect(screen.getAllByText(t).length).toBeGreaterThan(0);
    expect(screen.queryByText(/take (the )?(file|ledger)/i)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(current()).toEqual(["approved · current"]);
  });

  it("never renders either body", () => {
    q.prd = { data: { id: "P1", status: "approved", body: "LEDGER BODY TEXT" } };
    q.diff = { data: { pending: null, sections: [{ title: "S", state: "modified", lines: [{ op: "+", text: "DIFF LINE" }] }] } };
    at({ report: report({ conflicts: ["P1"], conflict_details: [c] }) });
    expect(screen.queryByText(/LEDGER BODY TEXT|DIFF LINE/)).toBeNull();
  });

  it("marks pending rebaseline current from the PRD's pending field, beside its status", () => {
    q.prd = { data: { id: "P1", status: "closed" } };
    q.diff = { data: { pending: { reason_type: "scope", reason: "r", requested_by: "u" }, sections: [] } };
    at({ report: report({ conflicts: ["P1"], conflict_details: [c] }) });
    expect(current()).toEqual(["closed · current", "pending rebaseline · current"]);
  });

  it("without a sync report (refresh, direct link) says it has no report and does not claim there is no conflict", () => {
    at(null);
    expect(screen.getByTestId("no-report")).toBeTruthy();
    expect(screen.queryByTestId("no-conflict")).toBeNull();
    expect(screen.queryByTestId("export-failed-not-conflict")).toBeNull();
    expect(screen.queryByText(/not a conflict|no hash conflict/i)).toBeNull();
  });

  it("an export failure with no conflict entry for the PRD is not shown as a conflict", () => {
    at({ report: report({ export_status: "failed" }) });
    expect(screen.getByTestId("export-failed-not-conflict")).toBeTruthy();
    expect(screen.queryByTestId("conflict-facts")).toBeNull();
  });

  it("an export failure alongside a real conflict still shows the conflict", () => {
    at({ report: report({ export_status: "failed", conflicts: ["P1"], conflict_details: [c] }) });
    expect(screen.getByTestId("conflict-facts")).toBeTruthy();
  });

  it("a report that lists the id without details does not say there is no conflict", () => {
    at({ report: report({ conflicts: ["P1"], conflict_details: undefined }) });
    expect(screen.getByTestId("conflict-no-detail")).toBeTruthy();
  });
});
