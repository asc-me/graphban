import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OrgDeployments } from "@/features/orgadmin/OrgDeployments";
import type { Deployment } from "@/lib/types";

/**
 * PRD-21 D6.
 *
 * The two things this screen must not do: offer a button that dead-ends in a connection
 * error, and collapse "never pushed" into "stale". The first is why the address is text
 * before it is a link — the console cannot probe the viewer's network. The second is two
 * different problems that need opposite responses.
 */
const dep = (over: Partial<Deployment> = {}): Deployment => ({
  label: "laptop — acme-core", credential_id: "key_1", prefix: "gb_sk_ab12",
  project_id: "prj_core", project_tag: "CORE", project_name: "Core",
  base_url: "http://ubuntu-srv:8080", last_push_at: new Date().toISOString(),
  node_count: 412, freshness: "in_sync", revoked: false,
  agents: [{ key: "CORE-A1", label: "worker-1", role: "worker", state: "online" }],
  ...over,
});

vi.mock("@/lib/api", () => ({
  api: {
    orgs: vi.fn(async () => [{ id: "org_1", name: "Acme", plan: "team", role: "owner" }]),
    deployments: vi.fn(async () => [dep()]),
  },
}));

function renderDeployments() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/org/admin/deployments"]}>
        <OrgDeployments />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Linked deployments", () => {
  beforeEach(async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.deployments).mockResolvedValue([dep()]);
    localStorage.clear();
  });

  it("shows the address as visible text, linked — not a bare button", async () => {
    // Someone on the wrong network can see `ubuntu-srv` is unreachable for them BEFORE
    // clicking. A button reading "Open deployment" would hide that until it failed.
    renderDeployments();
    const link = await screen.findByRole("link", { name: "http://ubuntu-srv:8080" });
    expect(link).toHaveAttribute("href", "http://ubuntu-srv:8080");
    expect(screen.queryByRole("button", { name: /open deployment/i })).not.toBeInTheDocument();
  });

  it("calls the address a hint rather than a guarantee", async () => {
    renderDeployments();
    expect(await screen.findByText(/hint rather than a guarantee/)).toBeInTheDocument();
  });

  it("says an unreported address is unreported, instead of inventing one", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.deployments).mockResolvedValue([dep({ base_url: "" })]);
    renderDeployments();
    expect(await screen.findByText(/has not told the cloud where it answers/))
      .toBeInTheDocument();
  });

  it("distinguishes never-pushed from stale", async () => {
    // A link nobody finished versus a box that stopped — opposite actions.
    const { api } = await import("@/lib/api");
    vi.mocked(api.deployments).mockResolvedValue([
      dep({ freshness: "never", last_push_at: null, node_count: 0 }),
    ]);
    renderDeployments();
    expect(await screen.findByText("never pushed")).toBeInTheDocument();
    expect(screen.getByText(/set up and not finished/)).toBeInTheDocument();
    expect(screen.queryByText(/the box stopped/)).not.toBeInTheDocument();
  });

  it("explains a stale deployment as a box that stopped", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.deployments).mockResolvedValue([dep({ freshness: "stale" })]);
    renderDeployments();
    expect(await screen.findByText("stale")).toBeInTheDocument();
    expect(screen.getByText(/the box stopped/)).toBeInTheDocument();
  });

  it("keeps a retired deployment visible rather than dropping it", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.deployments).mockResolvedValue([dep({ revoked: true })]);
    renderDeployments();
    expect(await screen.findByText("retired")).toBeInTheDocument();
    expect(screen.getByText("laptop — acme-core")).toBeInTheDocument();
  });

  it("remembers a per-user address override without changing what the box reported", async () => {
    renderDeployments();
    await userEvent.click(await screen.findByLabelText(/Edit address for/));
    await userEvent.type(screen.getByLabelText(/Address override for/), "http://10.0.0.4:8080");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByRole("link", { name: "http://10.0.0.4:8080" })).toBeInTheDocument();
    expect(screen.getByText("your override")).toBeInTheDocument();
    expect(localStorage.getItem("gb_deployment_url_overrides")).toContain("10.0.0.4");
  });

  it("says an idle project is idle, and why the cloud would know", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.deployments).mockResolvedValue([dep({ agents: [] })]);
    renderDeployments();
    expect(await screen.findByText(/because a linked box forwards its claims here/))
      .toBeInTheDocument();
  });

  it("says nothing is linked rather than showing an empty list", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.deployments).mockResolvedValue([]);
    renderDeployments();
    expect(await screen.findByText("Nothing is linked yet")).toBeInTheDocument();
  });

  it("calls the mint object a link key, not a sync credential", async () => {
    // API keys and Sync / Link mint a link key. This page used to say "sync credential"
    // for the same object — two names, one identity.
    const { api } = await import("@/lib/api");
    vi.mocked(api.deployments).mockResolvedValue([]);
    renderDeployments();
    expect(await screen.findByText(/No link key has been minted/)).toBeInTheDocument();
    expect(screen.getByText(/using a link key/)).toBeInTheDocument();
    expect(screen.queryByText(/sync credential/i)).not.toBeInTheDocument();
  });
});
