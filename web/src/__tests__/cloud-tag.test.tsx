import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CloudTag, cloudTagFor } from "@/components/shell/CloudTag";
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
    expect(tag.dataset.tooltip).toMatch(/^Syncs to CORE in Acme · last push \d\d:\d\d$/);
    expect(tag.outerHTML.toLowerCase()).not.toContain("b1d36410");
  });

  it("unknown tag: ☁ ? with its tooltip, never the id", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(
      status([proj("a", { cloud_project_tag: "", last_push_result: "ok", last_push_at: "2026-11-11T14:02:00" })]) as never,
    );
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveTextContent("☁ ?");
    expect(tag.dataset.tooltip).toBe("Couldn't read the cloud project's tag");
    expect(tag.outerHTML.toLowerCase()).not.toContain("b1d36410");
  });

  it("pending: mapped and never pushed, a hollow ring with a dashed border", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(status([proj("a", { last_push_result: null })]) as never);
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveAttribute("data-state", "pending");
    expect(tag.dataset.tooltip).toBe("Linked to CORE in Acme · not pushed yet");
    const dot = tag.querySelector("[data-dot]")!;
    expect(dot.className).toContain("border-dashed");
    expect(dot.className).toContain("bg-transparent");
    expect(dot.className).not.toMatch(/\bbg-(faint|muted|st-|\[)/);
  });

  it("the tooltip shows on keyboard focus, not only on hover", async () => {
    const user = userEvent.setup();
    vi.mocked(api.syncStatus).mockResolvedValue(status([proj("a", { last_push_result: null })]) as never);
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.tab();
    expect(tag).toHaveFocus();
    expect(screen.getByRole("tooltip")).toHaveTextContent("Linked to CORE in Acme · not pushed yet");
    expect(tag).toHaveAccessibleDescription("Linked to CORE in Acme · not pushed yet");
    await user.tab();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.hover(tag);
    expect(screen.getByRole("tooltip")).toBeVisible();
  });

  it("unknown: grey, never synced", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(
      status([proj("a", { last_push_result: "unknown", last_push_at: "2026-11-11T14:02:00" })]) as never,
    );
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveAttribute("data-state", "unknown");
    expect(tag.dataset.tooltip).toBe("Couldn't reach console.example.com · state unknown");
  });

  it("refused: red", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(status([proj("a", { last_push_result: "refused" })]) as never);
    renderTag("a");
    const tag = await screen.findByTestId("cloud-tag");
    expect(tag).toHaveAttribute("data-state", "refused");
    expect(tag.dataset.tooltip).toBe("Acme refused the last push · no access to CORE");
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

  it("a failed first read draws no tag: ☁ ? never lands on a project that may be local only", async () => {
    vi.mocked(api.syncStatus).mockRejectedValue(new Error("down"));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><div data-testid="slot"><CloudTag projectId="a" /></div></QueryClientProvider>);
    await waitFor(() => expect(qc.getQueryState(["sync-status"])?.status).toBe("error"));
    expect(screen.getByTestId("slot")).toBeEmptyDOMElement();
    expect(screen.queryByText("☁ ?")).toBeNull();
  });

  it("a failed refetch turns mapped tags unknown, never synced, and leaves local ones bare", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue(
      status([
        proj("a", { last_push_result: "ok", last_push_at: "2026-11-11T14:02:00" }),
        proj("b", { mapped: false, cloud_project_id: null, cloud_project_tag: "" }),
      ]) as never,
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <div data-testid="slot-a"><CloudTag projectId="a" /></div>
        <div data-testid="slot-b"><CloudTag projectId="b" /></div>
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("cloud-tag")).toHaveAttribute("data-state", "synced");
    vi.mocked(api.syncStatus).mockRejectedValue(new Error("down"));
    await qc.refetchQueries({ queryKey: ["sync-status"] });
    await waitFor(() => expect(screen.getByTestId("cloud-tag")).toHaveAttribute("data-state", "unknown"));
    expect(screen.getByTestId("cloud-tag").dataset.tooltip).toBe("Couldn't reach console.example.com · state unknown");
    expect(screen.getByTestId("slot-b")).toBeEmptyDOMElement();
  });

  it("cloudTagFor: no status → no tag; unreachable + local only → no tag", () => {
    expect(cloudTagFor("a", undefined)).toBeNull();
    const s = status([proj("b", { mapped: false })]) as never;
    expect(cloudTagFor("b", s, true)).toBeNull();
  });
});
