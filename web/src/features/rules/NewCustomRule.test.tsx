/** New custom rule form (GRPH-1810). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleStage } from "@/lib/types";

import { NewCustomRule, slugFromTitle } from "./NewCustomRule";

vi.mock("@/lib/api", () => ({ api: { rulePrimitiveContract: vi.fn(), createCustomRule: vi.fn() } }));

const contract = {
  api_version: "1",
  primitives: [
    { id: "evidence_required", stage: "Build", level: "enforced", params: [{ name: "engine", type: "str", required: false, comparator: null }] },
    { id: "found_bug", stage: "Build", level: "enforced", params: [] },
    { id: "exclusive_areas", stage: "Plan", level: "enforced", params: [{ name: "paths", type: "list", required: false, comparator: null }] },
  ],
};
const stage = (title: string) => ({ id: title.toLowerCase(), title, summary: "", state: "enforced", rules: [] }) as unknown as RuleStage;

function view(title: string, canWrite = true) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NewCustomRule projectId="core" stage={stage(title)} canWrite={canWrite} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.rulePrimitiveContract).mockResolvedValue(contract as never);
});

describe("NewCustomRule", () => {
  it("generates a hyphen-only id from the title", () => {
    expect(slugFromTitle("Migrations show Postgres")).toBe("migrations-show-postgres");
    expect(slugFromTitle("  a_b!! c ")).toBe("a-b-c");
  });

  it("offers exactly the stage's primitives from the contract", async () => {
    const user = userEvent.setup();
    view("Build");
    await user.click(await screen.findByTestId("rules-new-custom-open"));
    await waitFor(() => expect(screen.getByRole("option", { name: "found_bug" })).toBeTruthy());
    const opts = screen.getAllByRole("option").map((o) => o.textContent);
    expect(opts).toEqual(["evidence_required", "found_bug"]);
  });

  it("Plan offers only Plan's primitives and shows the live id", async () => {
    const user = userEvent.setup();
    view("Plan");
    await user.click(await screen.findByTestId("rules-new-custom-open"));
    await waitFor(() => expect(screen.getByRole("option", { name: "exclusive_areas" })).toBeTruthy());
    expect(screen.getAllByRole("option")).toHaveLength(1);
    await user.type(screen.getByLabelText("Title"), "Migrations show Postgres");
    expect(screen.getByTestId("rules-new-custom-id").textContent).toBe("custom.migrations-show-postgres");
  });

  it("saves, and shows a refusal verbatim", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createCustomRule).mockRejectedValueOnce(new Error("claims too strong"));
    view("Plan");
    await user.click(await screen.findByTestId("rules-new-custom-open"));
    await screen.findByRole("option", { name: "exclusive_areas" });
    await user.type(screen.getByLabelText("Title"), "No clash");
    await user.type(screen.getByLabelText("Why"), "lost a day");
    await user.type(screen.getByLabelText("paths (comma-separated)"), "a/, b/");
    await user.click(screen.getByTestId("rules-new-custom-save"));
    expect((await screen.findByTestId("rules-new-custom-error")).textContent).toContain("claims too strong");
    expect(api.createCustomRule).toHaveBeenCalledWith("core", expect.objectContaining({ slug: "no-clash", primitive: "exclusive_areas", params: { paths: ["a/", "b/"] } }));
  });

  it("shows members no control", () => {
    view("Plan", false);
    expect(screen.queryByTestId("rules-new-custom-open")).toBeNull();
  });
});
