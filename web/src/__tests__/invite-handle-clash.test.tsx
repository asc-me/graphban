import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { InviteAcceptPage } from "@/features/onboarding/InviteAcceptPage";
import { OrganizationsSection } from "@/features/profile/OrganizationsSection";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { User } from "@/lib/types";

const me = { id: "u1", email: "a@x.io", handle: "acain", name: "A" } as unknown as User;

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({ user: { email: "a@x.io", handle: "acain" }, logout: vi.fn() }),
}));

const clash = () =>
  new Error(JSON.stringify({ detail: { code: "handle_taken", org_id: "o", org_name: "Acme", suggested: "acain-2" } }));

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(keys.orgs, [{ id: "o2", name: "Beta", plan: "p", role: "owner" }]);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/invite/tok"]}>
        <Routes>
          <Route path="/invite/:token" element={<InviteAcceptPage />} />
          <Route path="/" element={<OrganizationsSection user={me} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return qc;
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "previewInvite").mockResolvedValue({
    kind: "org", org_name: "Acme", email: "a@x.io", role: "member", invited_by: "Bo",
  });
  vi.spyOn(api, "config").mockResolvedValue({ hosted_mode: true } as never);
});

describe("accepting an invite whose org already has your handle", () => {
  it("opens the org-only handle field with the suggestion, accepts with it, and the row shows it", async () => {
    const acme = { id: "o", name: "Acme", plan: "p", role: "member" as const, handle: "acain-2" };
    const accept = vi.spyOn(api, "acceptInvite").mockRejectedValueOnce(clash()).mockResolvedValueOnce(acme);
    const orgs = vi
      .spyOn(api, "orgs")
      .mockResolvedValue([{ id: "o2", name: "Beta", plan: "p", role: "owner" }, acme]);
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    const field = (await screen.findByLabelText(/Your handle in Acme/)) as HTMLInputElement;
    expect(field.value).toBe("acain-2");
    await userEvent.click(screen.getByRole("button", { name: "Accept with this handle" }));
    await waitFor(() => expect(accept).toHaveBeenLastCalledWith("tok", "acain-2"));
    // Back home, the Acme row carries the org-only handle; Beta carries none.
    expect(await screen.findByText("@acain-2")).toBeTruthy();
    expect(screen.getAllByText(/Change handle/)).toHaveLength(1);
    expect(orgs).toHaveBeenCalled();
  });

  it("shows why a retry failed when it is not another clash", async () => {
    vi.spyOn(api, "acceptInvite")
      .mockRejectedValueOnce(clash())
      .mockRejectedValueOnce(new Error("invitation expired"));
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    await screen.findByLabelText(/Your handle in Acme/);
    await userEvent.click(screen.getByRole("button", { name: "Accept with this handle" }));
    expect((await screen.findByRole("status")).textContent).toMatch(/expired/);
  });
});
