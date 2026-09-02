import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/features/orgadmin/Speculative", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/orgadmin/Speculative")>();
  return { ...actual, SPECULATIVE_ENABLED: true };
});

describe("Org roles vs fleet roles", () => {
  it("does not call the agent identity a credential", async () => {
    // This screen is people-roles. Fleet roles are a seat. "Enforced by credential"
    // was the Graphban API key wearing the LLM-credential word.
    const { OrgRoles } = await import("@/features/orgadmin/OrgRoles");
    render(<OrgRoles />);
    const note = screen.getByText(/roles for/i).closest("p");
    expect(note?.textContent).toMatch(/seat/i);
    expect(note?.textContent).not.toMatch(/credential/i);
  });
});
