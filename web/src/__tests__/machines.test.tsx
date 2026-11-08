import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MachinesPanel } from "@/features/settings/MachinesPanel";
import { api } from "@/lib/api";
import type { Machine } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { machines: vi.fn(), removeMachine: vi.fn() }, hasSession: () => true }));

const base: Machine = {
  id: "mch_1", machine_id: "gbm_1", label: "laptop", os: "Darwin 25", gban_version: "0.1.0",
  gbfleet_version: "0.2.0", harnesses: ["claude"], state: "clean", fail_count: 0, projects: {},
  first_reported_at: new Date().toISOString(), last_reported_at: new Date().toISOString(),
};

function renderPanel(rows: Machine[]) {
  vi.mocked(api.machines).mockResolvedValue(rows);
  vi.mocked(api.removeMachine).mockResolvedValue(undefined as never);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MachinesPanel /></QueryClientProvider>);
}

afterEach(() => cleanup());

describe("Settings → Machines (GRPH-1442)", () => {
  it("shows one state per machine: clean, failed with the count, and a stale 'last seen'", async () => {
    const old = new Date(Date.now() - 40 * 86400_000).toISOString();
    renderPanel([
      base,
      { ...base, id: "mch_2", label: "ubuntu", state: "failed", fail_count: 2 },
      { ...base, id: "mch_3", label: "old-box", last_reported_at: old },
    ]);
    expect((await screen.findAllByText("✓ CLEAN")).length).toBe(2);
    const failed = screen.getByText("× REPORTED · 2 FAIL");
    expect(failed.className).toContain("text-danger");
    expect(screen.getByText(/^last seen /)).toBeTruthy();
  });

  it("an empty list says never reported", async () => {
    renderPanel([]);
    expect(await screen.findByTestId("machines-empty")).toHaveTextContent("NEVER REPORTED");
  });

  it("two machines with the same label are two rows, and Remove calls the API", async () => {
    renderPanel([base, { ...base, id: "mch_9", machine_id: "gbm_9" }]);
    const rows = await screen.findAllByTestId("machine-row");
    expect(rows).toHaveLength(2);
    await userEvent.click(screen.getAllByRole("button", { name: "Remove laptop" })[1]);
    await waitFor(() => expect(api.removeMachine).toHaveBeenCalledWith("mch_9"));
  });
});
