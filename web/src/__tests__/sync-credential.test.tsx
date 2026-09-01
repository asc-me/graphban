import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsView } from "@/features/settings/SettingsView";
import { SyncCredentialInstall } from "@/features/settings/SyncCredentialInstall";

const api = vi.hoisted(() => ({
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({
    active: { id: "core", name: "Core" },
    projects: [
      { id: "core", name: "Core" },
      { id: "infra", name: "Infra" },
    ],
  }),
}));

vi.mock("@/lib/queries", () => ({
  keys: { apiKeys: ["api-keys"] },
  useConfig: () => ({ data: { hosted_mode: true, signup_mode: "invite_only" } }),
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

describe("SyncCredentialInstall", () => {
  it("builds the graphban link command with the pinned project", () => {
    const { container } = render(<SyncCredentialInstall apiKey="gb_sk_secret" projectId="core" />);
    // The <pre> is the copy target; prose elsewhere also mentions the command.
    const pre = container.querySelector("pre");
    // The exact flags matter — this is copy-pasted verbatim into a terminal.
    expect(pre?.textContent).toContain("graphban link");
    expect(pre?.textContent).toContain("--api-key gb_sk_secret");
    expect(pre?.textContent).toContain("--project core");
    expect(pre?.textContent).toContain("--cloud-url");
  });

  it("offers the local Settings → Sync/Link values as the other hand-off", async () => {
    const user = userEvent.setup();
    render(<SyncCredentialInstall apiKey="gb_sk_secret" projectId="core" />);
    await user.click(screen.getByRole("button", { name: /Local Settings/ }));
    expect(screen.getByText(/Sync API key\s+gb_sk_secret/)).toBeInTheDocument();
  });
});

describe("minting a sync credential", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.createApiKey.mockResolvedValue({ plaintext: "gb_sk_minted", project_id: "core" });
  });

  it("mints with the sync scope pinned to a project, not the default read/write", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("button", { name: "API Keys" }));
    await user.click(screen.getByRole("button", { name: "Sync credential" }));
    await user.type(screen.getByPlaceholderText(/laptop/), "laptop — core");
    await user.click(screen.getByRole("button", { name: /Mint credential/ }));

    await waitFor(() =>
      expect(api.createApiKey).toHaveBeenCalledWith(
        "laptop — core",
        "core",
        null,
        ["sync"],
        // A sync credential calls no MCP tools, so it is minted with no tier (GRPH-571).
        undefined,
      ),
    );
  });

  it("shows the link hand-off after minting, not the MCP snippet", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("button", { name: "API Keys" }));
    await user.click(screen.getByRole("button", { name: "Sync credential" }));
    await user.type(screen.getByPlaceholderText(/laptop/), "laptop");
    await user.click(screen.getByRole("button", { name: /Mint credential/ }));

    await waitFor(() =>
      expect(document.querySelector("pre")?.textContent).toContain("graphban link"),
    );
    expect(screen.queryByText(/Connect an agent · MCP/)).not.toBeInTheDocument();
  });

  it("leaves agent keys on the default scopes", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("button", { name: "API Keys" }));
    await user.type(screen.getByPlaceholderText(/ci-agent/), "ci-agent");
    await user.click(screen.getByRole("button", { name: /Create key/ }));

    await waitFor(() =>
      // undefined scopes → the backend default ["read","write"]; `[]` tiers → the core MCP
      // manifest, which is likewise the backend default (GRPH-571).
      expect(api.createApiKey).toHaveBeenCalledWith("ci-agent", "core", null, undefined, []),
    );
    // The snippet's default must match the key's default: a project-scoped key registers
    // project-scoped, without the operator translating between the two by hand.
    await waitFor(() =>
      expect(document.querySelector("pre.max-h-56")?.textContent).toContain("--scope project graphban"),
    );
  });

  it("mints an unbound key when the scope toggle says Global", async () => {
    // The old checkbox's job, as a Project|Global toggle: the ONLY difference a scope
    // choice may make is the project argument going null — same kind, same tiers.
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("button", { name: "API Keys" }));
    await user.click(screen.getByRole("button", { name: "Global" }));
    await user.type(screen.getByPlaceholderText(/ci-agent/), "fleet-wide");
    await user.click(screen.getByRole("button", { name: /Create key/ }));

    await waitFor(() =>
      expect(api.createApiKey).toHaveBeenCalledWith("fleet-wide", null, null, undefined, []),
    );
    // The toggle's whole point: an unbound key's connect command is user-scoped in the
    // harness, so no project file ever carries a key that outlives it.
    await waitFor(() =>
      expect(document.querySelector("pre.max-h-56")?.textContent).toContain("--scope user graphban"),
    );
  });
});
