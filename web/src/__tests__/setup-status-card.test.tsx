import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SetupStatusCard, onConnectStepRequested, setupStatusTiming } from "@/features/home/SetupStatusCard";
import { api } from "@/lib/api";
import type { ProjectSetup, SetupStep } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: { projectSetup: vi.fn(), machines: vi.fn(), removeMachine: vi.fn() },
  hasSession: () => true,
}));

function setup(step: Partial<SetupStep>): ProjectSetup {
  return {
    project_id: "core", offline: false, onboarding: null,
    steps: [{ step: 5, name: "Connect your agent", status: "pending", detail: "", how: "", ...step }],
  };
}

function renderCard(step: Partial<SetupStep>, extra: SetupStep[] = []) {
  const base = setup(step);
  vi.mocked(api.projectSetup).mockResolvedValue({ ...base, steps: [...extra, ...base.steps] });
  vi.mocked(api.machines).mockResolvedValue([]);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}><MemoryRouter><SetupStatusCard projectId="core" /></MemoryRouter></QueryClientProvider>,
  );
}

beforeEach(() => localStorage.clear());
afterEach(() => cleanup());

describe("dashboard Setup status card (GRPH-1442)", () => {
  it("proof: header names the machine that reported clean", async () => {
    renderCard({ status: "done", proof: "machine_report", machine: "laptop" });
    expect(await screen.findByText(/✓ Agent connection verified — laptop reported clean/)).toBeTruthy();
  });

  it("never reported: says so and gives the next command", async () => {
    renderCard({});
    const box = await screen.findByTestId("setup-status-never");
    expect(box).toHaveTextContent("No machine has reported yet");
    expect(screen.getByTestId("setup-status-card").textContent).not.toMatch(/this machine|that machine/i);
    expect(screen.getByText("$ gban setup")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
  });

  it("two machines: two rows, each with a copyable doctor command", async () => {
    renderCard({ machines: [
      { id: "a", machine: "laptop", state: "failed", fails: 1, unknowns: 0, at: "x", failing: [] },
      { id: "b", machine: "ubuntu", state: "clean", fails: 0, unknowns: 0, at: "y", failing: [] },
    ] });
    await screen.findAllByTestId("setup-status-failed");
    expect(screen.getAllByTestId(/setup-status-(failed|clean)/)).toHaveLength(2);
    expect(screen.getAllByText("$ gban doctor")).toHaveLength(2);
  });

  const where = (detail: string): SetupStep => ({ step: 0, name: "Where", status: "done", detail, how: "" });

  it("this computer: no curl line", async () => {
    renderCard({}, [where("this_computer")]);
    await screen.findByTestId("setup-status-never");
    expect(screen.queryByTestId("setup-status-curl")).toBeNull();
  });

  it("dedicated server: the curl line is shown", async () => {
    renderCard({}, [where("dedicated_server at http://srv:8080")]);
    expect(await screen.findByTestId("setup-status-curl")).toHaveTextContent("curl -fsS http://srv:8080/health");
  });

  it("Dismiss carries its consequence", async () => {
    renderCard({});
    await screen.findByRole("button", { name: "Dismiss" });
    expect(screen.getByText(/Hides this card\. Settings → Machines keeps the list\./)).toBeTruthy();
  });

  it("a failed report shows the count and failing lines in the failure colour, not a tick", async () => {
    renderCard({ reported: { machine: "ubuntu", fails: 1, at: "x", failing: [{ status: "FAIL", name: "gbfleet", detail: "missing" }] } });
    const box = await screen.findByTestId("setup-status-failed");
    expect(box).toHaveTextContent("ubuntu: last report x had 1 FAIL");
    expect(box).toHaveTextContent("FAIL gbfleet — missing");
    expect(box.querySelector("p")!.className).toContain("text-danger");
    expect(screen.queryByText(/Agent connection verified/)).toBeNull();
  });

  it("an UNKNOWN-only report is not a tick either, and says what could not be checked", async () => {
    renderCard({ machines: [{ id: "m1", machine: "ubuntu", state: "unknown", fails: 0, unknowns: 1, at: "x",
      failing: [{ status: "UNKNOWN", name: "gbfleet", detail: "not found" }] }] });
    const box = await screen.findByTestId("setup-status-failed");
    expect(box).toHaveTextContent("had 1 UNKNOWN");
    expect(box).toHaveTextContent("UNKNOWN gbfleet — not found");
    expect(box.querySelector("[data-state]")!.getAttribute("data-state")).toBe("unknown");
    expect(screen.queryByText(/Agent connection verified/)).toBeNull();
  });

  it("lists each machine separately with its own state", async () => {
    renderCard({ machines: [
      { id: "a", machine: "ubuntu", state: "failed", fails: 2, unknowns: 0, at: "x", failing: [] },
      { id: "b", machine: "ubuntu", state: "unknown", fails: 0, unknowns: 1, at: "y", failing: [] },
    ] });
    const rows = await screen.findAllByTestId("setup-status-failed");
    expect(rows).toHaveLength(2);
    expect(rows[0].querySelector("[data-state]")!.textContent).toBe("× REPORTED · 2 FAIL");
    expect(rows[0].querySelector("p")!.className).toContain("text-danger");
    expect(rows[1].querySelector("[data-state]")!.getAttribute("data-state")).toBe("unknown");
  });

  it("Verify with a key instead asks the shell to open the paste route", async () => {
    const seen = vi.fn();
    const off = onConnectStepRequested(seen);
    renderCard({});
    await userEvent.click(await screen.findByRole("button", { name: "Verify with a key instead" }));
    expect(seen).toHaveBeenCalledTimes(1);
    off();
  });

  it("Dismiss hides the card for good, across remounts", async () => {
    const first = renderCard({});
    await userEvent.click(await screen.findByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("setup-status-card")).toBeNull();
    first.unmount();
    renderCard({});
    await vi.waitFor(() => expect(vi.mocked(api.projectSetup).mock.calls.length).toBeGreaterThan(1));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId("setup-status-card")).toBeNull();
  });

  const job = (status: SetupStep["status"], n: number) =>
    [2, 3, 4].map((step) => ({ step, name: "c", status, detail: step === 4 ? `${n} item(s).` : "0 counted.", how: "" }));

  it("First job stays on the card with its three counters until ✓", async () => {
    renderCard({}, job("pending", 3));
    const row = await screen.findByTestId("setup-status-first-job");
    expect(row).toHaveTextContent("First job — pending");
    expect(screen.getByTestId("setup-status-count-items")).toHaveTextContent("3");
    expect(screen.getByTestId("setup-status-count-code-nodes")).toHaveTextContent("0");
  });

  it("First job shows under a verified connection too", async () => {
    renderCard({ status: "done", proof: "first_call" }, job("pending", 1));
    expect(await screen.findByTestId("setup-status-count-items")).toHaveTextContent("1");
  });

  it("the counter updates when an item is seeded, without a reload", async () => {
    const saved = setupStatusTiming.pollMs;
    setupStatusTiming.pollMs = 20;
    try {
      renderCard({ status: "done", proof: "first_call" }, job("pending", 0));
      expect(await screen.findByTestId("setup-status-count-items")).toHaveTextContent("0");
      const base = setup({ status: "done", proof: "first_call" });
      vi.mocked(api.projectSetup).mockResolvedValue({ ...base, steps: [...job("pending", 1), ...base.steps] });
      await vi.waitFor(() => expect(screen.getByTestId("setup-status-count-items")).toHaveTextContent("1"), { timeout: 1000 });
    } finally {
      setupStatusTiming.pollMs = saved;
    }
  });

  it("slows (does not hammer) once the connection and First job are both ✓", async () => {
    const saved = setupStatusTiming.pollMs;
    setupStatusTiming.pollMs = 20;
    try {
      renderCard({ status: "done", proof: "first_call" }, job("done", 5));
      await screen.findByText(/Agent connection verified/);
      await new Promise((r) => setTimeout(r, 60));
      const calls = vi.mocked(api.projectSetup).mock.calls.length;
      await new Promise((r) => setTimeout(r, 120));
      expect(vi.mocked(api.projectSetup).mock.calls.length).toBe(calls);
    } finally {
      setupStatusTiming.pollMs = saved;
    }
  });

  it("no First job row once it is ✓", async () => {
    renderCard({ status: "done", proof: "first_call" }, job("done", 5));
    await screen.findByText(/Agent connection verified/);
    expect(screen.queryByTestId("setup-status-first-job")).toBeNull();
  });
});

describe("API down (GRPH-1547)", () => {
  const job = (status: SetupStep["status"], n: number) =>
    [2, 3, 4].map((step) => ({ step, name: "c", status, detail: step === 4 ? `${n} item(s).` : "0 counted.", how: "" }));
  // The page nginx serves when the API container is stopped, padding comments and all.
  const html502 =
    "<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n<center><h1>502 Bad Gateway</h1></center>\r\n" +
    "<hr><center>nginx/1.27.0</center>\r\n</body>\r\n</html>\r\n<!-- a padding to disable MSIE and Chrome friendly error page -->\r\n";

  it("errorDetail reduces a 502 HTML page to one line; a 500 is not blamed on the proxy", async () => {
    const { errorDetail } = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    const d = errorDetail(502, html502);
    expect(d).toBe("the API is unreachable (502 from the proxy)");
    expect(d).not.toMatch(/</);
    expect(errorDetail(500, "<html>oops</html>")).toBe("the API returned an unreadable 500 response");
    expect(errorDetail(400, '{"detail":"x"}')).toBe('{"detail":"x"}');
  });

  it("both ✓, then the API goes down: the slow poll turns the card unknown with a one-line reason", async () => {
    // The real request path, against a fetch that answers like nginx with the API stopped.
    const real = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    const saved = { ...setupStatusTiming };
    setupStatusTiming.pollMs = 20;
    setupStatusTiming.provenPollMs = 40;
    let down = false;
    const fetchStub = vi.fn(async () =>
      down
        ? new Response(html502, { status: 502, headers: { "Content-Type": "text/html" } })
        : new Response(JSON.stringify({ ...setup({ status: "done", proof: "first_call" }), steps: [...job("done", 5), ...setup({ status: "done", proof: "first_call" }).steps] }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchStub);
    vi.mocked(api.projectSetup).mockImplementation((id: string) => real.api.projectSetup(id));
    vi.mocked(api.machines).mockResolvedValue([]);
    try {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(<QueryClientProvider client={qc}><MemoryRouter><SetupStatusCard projectId="core" /></MemoryRouter></QueryClientProvider>);
      await screen.findByText(/Agent connection verified/);
      expect(screen.queryByTestId("setup-status-first-job")).toBeNull();
      down = true;
      const card = await screen.findByText(/Setup status unknown/, {}, { timeout: 2000 });
      expect(card.textContent).toContain("the API is unreachable (502 from the proxy)");
      expect(screen.queryByText(/Agent connection verified/)).toBeNull();
      expect(document.body.textContent).not.toMatch(/Bad Gateway|nginx|padding|</);
    } finally {
      Object.assign(setupStatusTiming, saved);
      vi.unstubAllGlobals();
    }
  });
});
