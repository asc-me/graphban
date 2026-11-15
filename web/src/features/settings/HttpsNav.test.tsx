import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { SettingsView } from "@/features/settings/SettingsView";
import { settingsPath } from "@/lib/routes";

// GRPH-1636: the HTTPS entry sits in THIS BOX between Cloud / Sync and Gitops, self-host only.
const { configSpy } = vi.hoisted(() => ({
  configSpy: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => []),
    config: configSpy,
    httpsStatus: vi.fn(async () => ({
      tier: "box-ca", ready: true, max_names: 32, outside_constraints: [], covered_names: [],
      leaf_not_after: null, ca_not_after: null, ca_certificate_path: "/api/https/ca.crt", names: [],
    })),
    credentials: vi.fn(async () => []),
    platform: vi.fn(async () => null),
    syncStatus: vi.fn(async () => ({ linked: false, url: "", projects: [] })),
    members: vi.fn(async () => []),
    apiKeys: vi.fn(async () => []),
  },
}));

function renderAt(path: string, hosted: boolean) {
  configSpy.mockResolvedValue({ hosted_mode: hosted, signup_mode: "closed" });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="/settings/*" element={<SettingsView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("HTTPS settings nav entry", () => {
  beforeEach(() => vi.clearAllMocks());

  it("self-host lists HTTPS between Cloud / Sync and Gitops", async () => {
    renderAt(settingsPath("deployment/https"), false);
    const https = await screen.findByRole("link", { name: "HTTPS" });
    expect(https).toHaveAttribute("href", settingsPath("deployment/https"));
    const labels = screen.getAllByRole("link").map((a) => a.textContent);
    const i = labels.indexOf("HTTPS");
    expect(labels[i - 1]).toBe("Cloud / Sync");
    expect(labels[i + 1]).toBe("Gitops");
    expect(await screen.findByText("Trust this box on this device")).toBeInTheDocument();
  });

  it("hosted mode hides the HTTPS entry", async () => {
    renderAt(settingsPath("deployment/providers"), true);
    await screen.findByRole("link", { name: "Sync / Link" });
    expect(screen.queryByRole("link", { name: "HTTPS" })).toBeNull();
  });
});
