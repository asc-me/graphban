import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OrganizationsSection } from "@/features/profile/OrganizationsSection";
import { api } from "@/lib/api";
import type { User } from "@/lib/types";

const me = { id: "u1", handle: "acain", name: "A", email: "a@x.io" } as unknown as User;
const invite = {
  id: "inv1",
  token: "tok1",
  org_id: "o3",
  org_name: "Gamma",
  role: "member",
  invited_by: "Bo",
  created_at: "2026-11-20T00:00:00Z",
};

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <OrganizationsSection user={me} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return qc;
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "config").mockResolvedValue({ hosted_mode: true } as never);
});

describe("organizations section — invitations and leave", () => {
  it("accepts an invitation through the handle clash and the row shows the org-only handle", async () => {
    const orgs = vi.spyOn(api, "orgs").mockResolvedValue([] as never);
    const invites = vi.spyOn(api, "myInvites").mockResolvedValue([invite] as never);
    const clash = new Error(
      JSON.stringify({ detail: { code: "handle_taken", org_id: "o3", org_name: "Gamma", suggested: "acain-2" } }),
    );
    const accept = vi
      .spyOn(api, "acceptInvite")
      .mockRejectedValueOnce(clash)
      .mockResolvedValueOnce({ id: "o3", name: "Gamma", plan: "p", role: "member", handle: "acain-2" } as never);
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "Accept" }));
    const field = await screen.findByLabelText(/Your handle in Gamma/);
    expect((field as HTMLInputElement).value).toBe("acain-2");
    orgs.mockResolvedValue([{ id: "o3", name: "Gamma", plan: "p", role: "member", handle: "acain-2" }] as never);
    invites.mockResolvedValue([] as never);
    await userEvent.click(screen.getByRole("button", { name: "Accept with this handle" }));
    await waitFor(() => expect(accept).toHaveBeenLastCalledWith("tok1", "acain-2"));
    expect(await screen.findByText("@acain-2")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
  });

  it("declines an invitation", async () => {
    vi.spyOn(api, "orgs").mockResolvedValue([] as never);
    const invites = vi.spyOn(api, "myInvites").mockResolvedValue([invite] as never);
    const decline = vi.spyOn(api, "declineMyInvite").mockResolvedValue(undefined as never);
    mount();
    const btn = await screen.findByRole("button", { name: "Decline" });
    invites.mockResolvedValue([] as never);
    await userEvent.click(btn);
    await waitFor(() => expect(decline).toHaveBeenCalledWith("inv1"));
    await waitFor(() => expect(screen.queryByText("Gamma")).toBeNull());
  });

  it("disables Leave for the only owner with the reason inline, and leaves otherwise", async () => {
    vi.spyOn(api, "myInvites").mockResolvedValue([] as never);
    vi.spyOn(api, "orgs").mockResolvedValue([
      { id: "o1", name: "Acme", plan: "p", role: "owner" },
      { id: "o2", name: "Beta", plan: "p", role: "member" },
    ] as never);
    vi.spyOn(api, "orgMembers").mockResolvedValue([{ user: me, role: "owner", projects: [] }] as never);
    const leave = vi.spyOn(api, "leaveOrg").mockResolvedValue({ removed_role: "member", projects_revoked: [] } as never);
    mount();
    expect(await screen.findByText(/only owner/)).toBeTruthy();
    const buttons = screen.getAllByRole("button", { name: "Leave" }) as HTMLButtonElement[];
    expect(buttons.map((b) => b.disabled)).toEqual([true, false]);
    await userEvent.click(buttons[1]);
    await userEvent.click(screen.getByRole("button", { name: "Confirm leave" }));
    await waitFor(() => expect(leave).toHaveBeenCalledWith("o2"));
  });
});
