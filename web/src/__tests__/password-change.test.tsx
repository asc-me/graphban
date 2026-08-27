import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
  useApiKeys: () => ({ data: [] }),
  useMembers: () => ({ data: [] }),
  usePlatform: () => ({ data: null }),
  // The settings view now also renders the deployment credentials panel (PRD-25 S5), so a
  // whole-module mock has to answer for its hooks too.
  useCredentials: () => ({ data: { credentials: [] }, isLoading: false }),
  useReindexStatus: () => ({ data: { running: false, tables: [] } }),
  // The credentials panel colours each project tag with that project's accent (PRD-25 S5).
  useProjects: () => ({ data: [] }),
}));

function renderSettings() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsView />
    </QueryClientProvider>,
  );
}

async function openAccountTab() {
  const user = userEvent.setup();
  renderSettings();
  await user.click(screen.getByRole("button", { name: "Account" }));
  return user;
}

/**
 * There was no password UI at all before GRPH-219 — and no endpoint behind the missing
 * button. The operator provisioned by `bootstrap-hosted` is handed a generated password,
 * which makes this the first thing they need and the one credential most likely to have
 * been pasted somewhere it shouldn't live.
 */
describe("Account → password", () => {
  beforeEach(() => {
    api.changePassword.mockReset();
    api.changePassword.mockResolvedValue(undefined);
  });

  it("sends the change and confirms it", async () => {
    const user = await openAccountTab();

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
    const user = await openAccountTab();

    await user.type(screen.getByPlaceholderText("Current password"), "old-password");
    await user.type(screen.getByPlaceholderText("New password"), "a-new-password");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "a-new-passwrod");

    expect(screen.getByRole("button", { name: /Change password/ })).toBeDisabled();
    expect(screen.getByText(/don’t match/)).toBeInTheDocument();
    expect(api.changePassword).not.toHaveBeenCalled();
  });

  it("will not submit a password shorter than the signup floor", async () => {
    const user = await openAccountTab();

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
    const user = await openAccountTab();

    await user.type(screen.getByPlaceholderText("Current password"), "wrong");
    await user.type(screen.getByPlaceholderText("New password"), "a-new-password");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "a-new-password");
    await user.click(screen.getByRole("button", { name: /Change password/ }));

    expect(await screen.findByText(/current password is incorrect/)).toBeInTheDocument();
  });
});
