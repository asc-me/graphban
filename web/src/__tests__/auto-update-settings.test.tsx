import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AutoUpdateSettings } from "@/features/settings/AutoUpdateSettings";
import type { AutoUpdateStatus } from "@/lib/types";

function status(over: Partial<AutoUpdateStatus> = {}): AutoUpdateStatus {
  return {
    available: true, unavailable_reason: "", enabled: false, timezone: "UTC",
    check: { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", end: "05:00" },
    install: { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", end: "05:00" },
    next_check_at: null, last_check_at: null, last_check_state: "",
    last_auto_install_tag: "", last_auto_install_at: null, last_auto_install_outcome: "",
    last_auto_install_error: "",
    ...over,
  };
}

const { getSpy, patchSpy } = vi.hoisted(() => ({
  getSpy: vi.fn(),
  patchSpy: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: { autoUpdate: getSpy, updateAutoUpdate: patchSpy } }));

function renderIt(progressError = "") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AutoUpdateSettings progressError={progressError} />
    </QueryClientProvider>,
  );
}

describe("AutoUpdateSettings", () => {
  beforeEach(() => {
    getSpy.mockReset().mockResolvedValue(status());
    patchSpy.mockReset().mockImplementation(async (b) => status({ enabled: !!b.enabled }));
  });

  it("saves the toggle, timezone and window editors", async () => {
    const user = userEvent.setup();
    renderIt();
    const toggle = await screen.findByRole("checkbox", { name: /update this box automatically/i });
    await user.click(toggle);
    const tz = screen.getByLabelText("Timezone");
    await user.clear(tz);
    await user.type(tz, "America/New_York");
    await user.click(within_group("Check window days", "Sun"));
    await user.click(screen.getByRole("button", { name: /save auto-update/i }));
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1));
    const body = patchSpy.mock.calls[0][0];
    expect(body.enabled).toBe(true);
    expect(body.timezone).toBe("America/New_York");
    expect(body.check_days).toEqual([0, 1, 2, 3, 4, 5]);
    expect(body.install_days).toEqual([0, 1, 2, 3, 4, 5, 6]);
    expect(body.check_start).toBe("02:00");
    expect(body.install_end).toBe("05:00");
  });

  it("shows a save error", async () => {
    patchSpy.mockRejectedValue(new Error("'Mars/Base' is not a known timezone"));
    const user = userEvent.setup();
    renderIt();
    await user.click(await screen.findByRole("button", { name: /save auto-update/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a known timezone/);
  });

  it.each([
    ["hosted", "hosted instances are updated by the operator"],
    ["no helper", "no apply path — the compose helper is not running"],
  ])("a %s box shows the control disabled with the reason", async (_n, reason) => {
    getSpy.mockResolvedValue(status({ available: false, unavailable_reason: reason }));
    renderIt();
    expect(await screen.findByText(new RegExp(reason))).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /update this box automatically/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /save auto-update/i })).toBeDisabled();
  });

  it("shows next check, last checked and the last auto-update", async () => {
    getSpy.mockResolvedValue(status({
      enabled: true,
      next_check_at: "2026-11-03T02:00:00+00:00",
      last_check_at: "2026-11-02T02:00:00+00:00",
      last_check_state: "available",
      last_auto_install_tag: "2026.11.1",
      last_auto_install_at: "2026-11-02T03:00:00+00:00",
      last_auto_install_outcome: "succeeded",
    }));
    renderIt();
    expect(await screen.findByText(/next check at/i)).toHaveTextContent(/Nov 3, 2026/);
    expect(screen.getByText(/last checked/i)).toHaveTextContent(/available/);
    expect(screen.getByText(/last auto-update/i)).toHaveTextContent(/2026\.11\.1 succeeded/);
  });

  it("a failed auto-install is shown prominently with the progress error", async () => {
    getSpy.mockResolvedValue(status({
      enabled: true, last_auto_install_tag: "2026.11.1", last_auto_install_outcome: "failed",
      last_auto_install_error: "stale",
    }));
    renderIt("!! build broke");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/2026\.11\.1 failed: !! build broke/);
  });
});

function within_group(groupName: string, day: string) {
  const group = screen.getByRole("group", { name: groupName });
  return Array.from(group.querySelectorAll("label")).find((l) => l.textContent === day)!
    .querySelector("input")!;
}
