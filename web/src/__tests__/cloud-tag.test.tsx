import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CloudTag } from "@/components/shell/CloudTag";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({ api: { syncStatus: vi.fn() } }));

// GRPH-1562: the id is a UUID unlike the tag, so rendering the id fails these tests.
const CLOUD_ID = "b1d36410-76d9-4c2e-9a51-0f3e2a7c1d01";
const proj = (id: string, extra: Record<string, unknown>) => ({
  project_id: id, name: id, writable: false, sync_graph: true, total_nodes: 0, synced_nodes: 0,
  pending: 0, last_synced_at: null, status: "live", mapped: true, cloud_project_id: CLOUD_ID,
  cloud_project_tag: "core", ...extra,
});
const status = (projects: unknown[], extra: Record<string, unknown> = {}) => ({
  linked: true, source: "web", cloud_url: "https://console.example.com", org: "Acme",
  credential_set: true, linked_at: null, can_link: false, projects, ...extra,
});

function renderTag(id: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={qc}><CloudTag projectId={id} /></QueryClientProvider>);
}

describe("cloud tag", () => {
  beforeEach(() => vi.clearAllMocks());

  it("synced: lit with last push", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(
      status([proj("a", { last_push_result: "ok", last_push_at: "2026-11-11T14:02:00" })]) as never,
    );
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveTextContent("☁ CORE");
    expect(tag).toHaveAttribute("data-state", "synced");
    expect(tag.title).toMatch(/^Syncs to CORE in Acme · last push \d\d:\d\d$/);
    expect(tag.outerHTML.toLowerCase()).not.toContain("b1d36410");
  });

  it("unknown tag: ☁ ? with its tooltip, never the id", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(
      status([proj("a", { cloud_project_tag: "", last_push_result: "ok", last_push_at: "2026-11-11T14:02:00" })]) as never,
    );
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveTextContent("☁ ?");
    expect(tag.title).toBe("Couldn't read the cloud project's tag");
    expect(tag.outerHTML.toLowerCase()).not.toContain("b1d36410");
  });

  it("pending: mapped and never pushed", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(status([proj("a", { last_push_result: null })]) as never);
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveAttribute("data-state", "pending");
    expect(tag.title).toBe("Linked to CORE in Acme · not pushed yet");
  });

  it("unknown: grey, never synced", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(
      status([proj("a", { last_push_result: "unknown", last_push_at: "2026-11-11T14:02:00" })]) as never,
    );
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveAttribute("data-state", "unknown");
    expect(tag.title).toBe("Couldn't reach console.example.com · state unknown");
  });

  it("refused: red", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(status([proj("a", { last_push_result: "refused" })]) as never);
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveAttribute("data-state", "refused");
    expect(tag.title).toBe("Acme refused the last push · no access to CORE");
  });

  it("local only: no tag and no local badge", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(
      status([proj("a", { mapped: false, cloud_project_id: null }), proj("b", { last_push_result: "ok" })]) as never,
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <div data-testid="slot-a"><CloudTag projectId="a" /></div>
        <div data-testid="slot-b"><CloudTag projectId="b" /></div>
      </QueryClientProvider>,
    );
    // The mapped sibling's tag proves the status query has resolved, so null for "a" is a real answer.
    await within(screen.getByTestId("slot-b")).findByTestId("cloud-tag");
    expect(within(screen.getByTestId("slot-a")).queryByTestId("cloud-tag")).toBeNull();
    expect(screen.getByTestId("slot-a")).toBeEmptyDOMElement();
    expect(screen.queryByText(/local/i)).toBeNull();
  });

  it("a failed status read shows unknown, never synced", async () => {
    vi.mocked(api.syncStatus).mockRejectedValue(new Error("down"));
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveAttribute("data-state", "unknown");
  });
});
