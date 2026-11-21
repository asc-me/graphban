import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OrganizationsSection } from "@/features/profile/OrganizationsSection";
import { handleClash } from "@/features/profile/orgHandle";
import { api } from "@/lib/api";
import type { User } from "@/lib/types";

const user = { id: "u1", handle: "acain", name: "A", email: "a@x.io" } as unknown as User;

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <OrganizationsSection user={user} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.restoreAllMocks());

describe("organizations section", () => {
  it("parses a handle_taken clash and ignores other errors", () => {
    const body = { detail: { code: "handle_taken", org_id: "o", org_name: "Acme", suggested: "acain-2" } };
    expect(handleClash(new Error(JSON.stringify(body)))?.suggested).toBe("acain-2");
    expect(handleClash(new Error("nope"))).toBeNull();
  });

  it("shows the Link this box empty state on an unlinked local box", async () => {
    vi.spyOn(api, "config").mockResolvedValue({ hosted_mode: false } as never);
    vi.spyOn(api, "syncStatus").mockResolvedValue({ linked: false, projects: [] } as never);
    mount();
    expect(await screen.findByText("Link this box")).toBeTruthy();
  });

  it("shows an org-only handle only on the row that has one, and changes it", async () => {
    vi.spyOn(api, "config").mockResolvedValue({ hosted_mode: true } as never);
    vi.spyOn(api, "orgs").mockResolvedValue([
      { id: "o1", name: "Acme", plan: "p", role: "member", handle: "acain-2" },
      { id: "o2", name: "Beta", plan: "p", role: "owner" },
    ] as never);
    const set = vi.spyOn(api, "setOrgHandle").mockResolvedValue({} as never);
    mount();
    expect(await screen.findByText("@acain-2")).toBeTruthy();
    expect(screen.getAllByText(/Change handle/)).toHaveLength(1);
    await userEvent.click(screen.getByText("Change handle"));
    const input = screen.getByLabelText(/Your handle in Acme/);
    await userEvent.clear(input);
    await userEvent.type(input, "acme-ac");
    await userEvent.click(screen.getAllByRole("button", { name: "Change handle" })[0]);
    await waitFor(() => expect(set).toHaveBeenCalledWith("o1", "acme-ac"));
  });
});
