import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { InviteAcceptPage } from "@/features/onboarding/InviteAcceptPage";
import { api } from "@/lib/api";

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({ user: { email: "a@x.io", handle: "acain" }, logout: vi.fn() }),
}));

beforeEach(() => vi.restoreAllMocks());

describe("accepting an invite whose org already has your handle", () => {
  it("opens the org-only handle field with the suggestion and accepts with it", async () => {
    vi.spyOn(api, "previewInvite").mockResolvedValue({
      kind: "org", org_name: "Acme", email: "a@x.io", role: "member", invited_by: "Bo",
    });
    const accept = vi
      .spyOn(api, "acceptInvite")
      .mockRejectedValueOnce(
        new Error(JSON.stringify({ detail: { code: "handle_taken", org_id: "o", org_name: "Acme", suggested: "acain-2" } })),
      )
      .mockResolvedValueOnce({} as never);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/invite/tok"]}>
          <Routes>
            <Route path="/invite/:token" element={<InviteAcceptPage />} />
            <Route path="/" element={<div>home</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Accept invitation" }));
    const field = (await screen.findByLabelText(/Your handle in Acme/)) as HTMLInputElement;
    expect(field.value).toBe("acain-2");
    await userEvent.click(screen.getByRole("button", { name: "Accept with this handle" }));
    await waitFor(() => expect(accept).toHaveBeenLastCalledWith("tok", "acain-2"));
    expect(await screen.findByText("home")).toBeTruthy();
  });
});
