import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
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
      <MemoryRouter>
        <SettingsView />
      </MemoryRouter>
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
    expect(screen.getByText(/Link key\s+gb_sk_secret/)).toBeInTheDocument();
    expect(screen.queryByText(/Sync API key/)).not.toBeInTheDocument();
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
    await user.click(screen.getByRole("link", { name: "API keys" }));
    await user.click(screen.getByRole("button", { name: "Link key" }));
    await user.type(screen.getByPlaceholderText(/laptop/), "laptop — core");
    await user.click(screen.getByRole("button", { name: /Mint link key/ }));

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
    await user.click(screen.getByRole("link", { name: "API keys" }));
    await user.click(screen.getByRole("button", { name: "Link key" }));
    await user.type(screen.getByPlaceholderText(/laptop/), "laptop");
    await user.click(screen.getByRole("button", { name: /Mint link key/ }));

    await waitFor(() =>
      expect(document.querySelector("pre")?.textContent).toContain("graphban link"),
    );
    expect(screen.queryByText(/Connect an agent · MCP/)).not.toBeInTheDocument();
  });

  it("leaves agent keys on the default scopes", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("link", { name: "API keys" }));
    expect(screen.getByPlaceholderText("Agent key name (e.g. claude-code)")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Key name (e.g. claude-code)")).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText(/claude-code/), "ci-agent");
    await user.click(screen.getByRole("button", { name: /Mint agent key/ }));

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
    await user.click(screen.getByRole("link", { name: "API keys" }));
    await user.click(screen.getByRole("button", { name: "Global" }));
    await user.type(screen.getByPlaceholderText(/claude-code/), "fleet-wide");
    await user.click(screen.getByRole("button", { name: /Mint agent key/ }));

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

describe("hosted Sync / Link is the cloud-org mint, not the self-host paste form", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.createApiKey.mockResolvedValue({ plaintext: "gb_sk_minted", project_id: "core" });
  });

  it("API Keys is a path, so the pane is keys not the Settings catch-all", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("link", { name: "API keys" }));
    expect(await screen.findByRole("button", { name: /Mint agent key/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^credentials$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Cloud link$/ })).not.toBeInTheDocument();
  });

  it("does not tell a cloud org to connect as if it were a self-hosted box", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("link", { name: "Sync / Link" }));

    expect(await screen.findByRole("heading", { name: /^Cloud link$/ })).toBeInTheDocument();
    expect(screen.getByText(/Mint a link key/)).toBeInTheDocument();
    expect(screen.getByText(/scoped link key/i)).toBeInTheDocument();
    // Same leftover as API keys: the name field said "Key name" next to Mint a link key.
    expect(screen.getByPlaceholderText("Link key name (e.g. laptop — acme-core)")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Key name (e.g. laptop — acme-core)")).not.toBeInTheDocument();
    expect(screen.queryByText(/scoped credential/i)).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("cloud.graphban.dev")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("paste link key…")).not.toBeInTheDocument();
    expect(screen.queryByText(/Connect this self-hosted instance/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Link instance" })).not.toBeInTheDocument();
  });

  it("mints a sync-scoped key pinned to a project", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("link", { name: "Sync / Link" }));
    await user.type(screen.getByPlaceholderText(/laptop/), "laptop — core");
    await user.click(screen.getByRole("button", { name: /Mint link key/ }));

    await waitFor(() =>
      expect(api.createApiKey).toHaveBeenCalledWith("laptop — core", "core", null, ["sync"]),
    );
    await waitFor(() =>
      expect(document.querySelector("pre")?.textContent).toContain("graphban link"),
    );
    expect(document.querySelector("pre")?.textContent).toContain("--api-key gb_sk_minted");
  });
});
