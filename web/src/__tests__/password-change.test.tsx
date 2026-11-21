import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PasswordCard } from "@/features/profile/PasswordCard";
import { SettingsView } from "@/features/settings/SettingsView";

const api = vi.hoisted(() => ({
  changePassword: vi.fn(),
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({
    active: { id: "core", name: "Core" },
    activeId: "core",
    projects: [{ id: "core", name: "Core" }],
  }),
}));

vi.mock("@/lib/queries", () => ({
  keys: { apiKeys: ["api-keys"] },
  useConfig: () => ({ data: { hosted_mode: true, signup_mode: "invite_only" } }),
  useApiKeys: () => ({ data: [] }),
  useMembers: () => ({ data: [] }),
  usePlatform: () => ({ data: null }),
  useCredentials: () => ({ data: { credentials: [] }, isLoading: false }),
  useReindexStatus: () => ({ data: { running: false, tables: [] } }),
  useModelLoads: (_projectId: string) => ({ data: undefined }),
  useProjects: () => ({ data: [] }),
}));

function wrap(ui: React.ReactNode, path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

// Password moved from Settings → Account to the profile page (GRPH-1792).
async function openPasswordCard() {
  const user = userEvent.setup();
  wrap(<PasswordCard />);
  return user;
}

describe("Settings → Account is gone", () => {
  it("has no Account entry in the settings nav", () => {
    wrap(<SettingsView />, "/settings/project/members");
    expect(screen.queryByRole("link", { name: "Account" })).not.toBeInTheDocument();
  });

  it("sends the old Account route to the profile page", () => {
    wrap(
      <Routes>
        <Route path="/settings/*" element={<SettingsView />} />
        <Route path="/profile" element={<div>profile page</div>} />
      </Routes>,
      "/settings/account",
    );
    expect(screen.getByText("profile page")).toBeInTheDocument();
  });
});

/**
 * There was no password UI at all before GRPH-219 — and no endpoint behind the missing
 * button. The operator provisioned by `bootstrap-hosted` is handed a generated password,
 * which makes this the first thing they need and the one credential most likely to have
 * been pasted somewhere it shouldn't live.
 */
describe("Profile → password", () => {
  beforeEach(() => {
    api.changePassword.mockReset();
    api.changePassword.mockResolvedValue(undefined);
  });

  it("sends the change and confirms it", async () => {
    const user = await openPasswordCard();

    await user.type(screen.getByPlaceholderText("Current password"), "old-password");
    await user.type(screen.getByPlaceholderText("New password"), "a-new-password");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "a-new-password");
    await user.click(screen.getByRole("button", { name: /Change password/ }));

    await waitFor(() =>
      expect(api.changePassword).toHaveBeenCalledWith("old-password", "a-new-password"),
    );
    expect(await screen.findByText(/Other devices are signed out/)).toBeInTheDocument();
  });

  it("will not submit when the confirmation does not match", async () => {
    const user = await openPasswordCard();

    await user.type(screen.getByPlaceholderText("Current password"), "old-password");
    await user.type(screen.getByPlaceholderText("New password"), "a-new-password");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "a-new-passwrod");

    expect(screen.getByRole("button", { name: /Change password/ })).toBeDisabled();
    expect(screen.getByText(/don’t match/)).toBeInTheDocument();
    expect(api.changePassword).not.toHaveBeenCalled();
  });

  it("will not submit a password shorter than the signup floor", async () => {
    const user = await openPasswordCard();

    await user.type(screen.getByPlaceholderText("Current password"), "old-password");
    await user.type(screen.getByPlaceholderText("New password"), "short");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "short");

    expect(screen.getByRole("button", { name: /Change password/ })).toBeDisabled();
    expect(api.changePassword).not.toHaveBeenCalled();
  });

  it("shows the server's reason when the current password is wrong", async () => {
    // The one failure a user will actually hit, and the one where a generic message
    // ("something went wrong") sends them to reset a password they in fact know.
    api.changePassword.mockRejectedValue(
      new Error(JSON.stringify({ detail: "current password is incorrect" })),
    );
    const user = await openPasswordCard();

    await user.type(screen.getByPlaceholderText("Current password"), "wrong");
    await user.type(screen.getByPlaceholderText("New password"), "a-new-password");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "a-new-password");
    await user.click(screen.getByRole("button", { name: /Change password/ }));

    expect(await screen.findByText(/current password is incorrect/)).toBeInTheDocument();
  });
});
