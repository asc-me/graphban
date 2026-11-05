import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ConflictRecordView } from "@/features/prds/ConflictRecordView";

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ active: { id: "core", name: "Core" }, activeId: "core", projects: [{ id: "core", name: "Core" }], setActiveId: () => {} }),
}));

const c = { prd_id: "P1", status: "approved", ledger_hash: "aaa", file_hash: "bbb", file_name: "p1.md", byte_length: 42, path: "prds/p1.md" };

function at(state: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/prds/conflict/P1", state }]}>
      <Routes><Route path="/prds/conflict/:id" element={<ConflictRecordView />} /></Routes>
    </MemoryRouter>,
  );
}

describe("ConflictRecordView", () => {
  it("shows both hashes and file facts, with no take-file or take-ledger", () => {
    at({ conflicts: [c] });
    for (const t of ["aaa", "bbb", "p1.md", "42", "prds/p1.md", "approved"]) expect(screen.getAllByText(t).length).toBeGreaterThan(0);
    expect(screen.queryByText(/take (the )?(file|ledger)/i)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });
  it("is not a conflict without a recorded hash conflict", () => {
    at({ conflicts: [] });
    expect(screen.getByTestId("no-conflict")).toBeTruthy();
  });
});
