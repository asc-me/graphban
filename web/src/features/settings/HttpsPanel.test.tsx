import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpsPanel, httpsAddress } from "@/features/settings/HttpsPanel";
import { api } from "@/lib/api";
import type { HttpsStatus } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: { httpsStatus: vi.fn(), httpsAddName: vi.fn(), httpsRemoveName: vi.fn() },
}));

const status = (over: Partial<HttpsStatus> = {}): HttpsStatus => ({
  tier: "box-ca", ready: true, max_names: 32, outside_constraints: [], covered_names: [],
  leaf_not_after: "2027-01-02T00:00:00+00:00", ca_not_after: "2036-01-02T00:00:00+00:00",
  ca_certificate_path: "/api/https/ca.crt",
  names: [
    { name: "192.168.1.5", source: "auto", origin: "network interface" },
    { name: "box.home", source: "operator", origin: "" },
  ], ...over,
});

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><HttpsPanel /></QueryClientProvider>);
}

beforeEach(() => { vi.mocked(api.httpsStatus).mockResolvedValue(status()); });
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

describe("HttpsPanel", () => {
  it("shows an API error as an error, not an empty names list", async () => {
    vi.mocked(api.httpsStatus).mockRejectedValue(new Error("boom"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(screen.queryByText(/0 of 32/)).toBeNull();
    expect(screen.queryByTestId("trust-chip")).toBeNull();
  });

  it("never shows Trusted when the probe rejects", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("cert")));
    mount();
    await screen.findByTestId("trust-chip");
    await userEvent.click(screen.getAllByRole("button", { name: "Check again" })[0]);
    await waitFor(() => expect(screen.getByTestId("trust-chip")).toHaveTextContent("Can't tell"));
    expect(screen.getByText(/The check from this browser failed/)).toBeInTheDocument();
    expect(screen.getByTestId("trust-refused")).toBeInTheDocument();
  });

  it("shows Trusted after a successful probe", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({}));
    mount();
    await screen.findByTestId("trust-chip");
    expect(screen.getByTestId("trust-chip")).toHaveTextContent("Can't tell");
    await userEvent.click(screen.getAllByRole("button", { name: "Check again" })[0]);
    await waitFor(() => expect(screen.getByTestId("trust-chip")).toHaveTextContent("Trusted by this browser"));
  });

  it("starts at Can't tell on an https page before any probe has run", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    vi.stubGlobal("location", { ...window.location, protocol: "https:", hostname: "box.home",
      host: "box.home:8443", origin: "https://box.home:8443" });
    mount();
    const chip = await screen.findByTestId("trust-chip");
    expect(chip).toHaveTextContent("Can't tell");
    expect(chip).not.toHaveTextContent("Trusted by this browser");
    expect(chip).toHaveAttribute("data-trust", "unknown");
    expect(screen.queryByText(/The check from this browser failed/)).toBeNull();
    expect(screen.queryByTestId("trust-refused")).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("keeps the port in the HTTPS address", () => {
    expect(httpsAddress({ protocol: "https:", hostname: "box.home", origin: "https://box.home:8443" }))
      .toBe("https://box.home:8443");
    expect(httpsAddress({ protocol: "http:", hostname: "box.home", origin: "http://box.home:8080" }, 8443))
      .toBe("https://box.home:8443");
    expect(httpsAddress({ protocol: "http:", hostname: "box.home", origin: "http://box.home:8080" }))
      .toBe("https://box.home");
  });

  it("renders names with Remove only on operator rows", async () => {
    mount();
    expect(await screen.findByText("found automatically (network interface)")).toBeInTheDocument();
    expect(screen.getByText("added by you")).toBeInTheDocument();
    expect(screen.getByText("2 of 32")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(1);
  });

  it("disables adding at 32 of 32", async () => {
    const names = Array.from({ length: 32 }, (_, i) => ({ name: `h${i}.lan`, source: "operator", origin: "" }));
    vi.mocked(api.httpsStatus).mockResolvedValue(status({ names }));
    mount();
    expect(await screen.findByText("32 of 32")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Add a name"), "x.lan");
    expect(screen.getByRole("button", { name: "Add a name" })).toBeDisabled();
  });

  it("shows the OS steps, the redirect line, and no private key", async () => {
    const { container } = mount();
    await screen.findByText("Trust this box on this device");
    await userEvent.click(screen.getByRole("tab", { name: "Windows" }));
    expect(screen.getByText(/Trusted Root Certification Authorities/)).toBeInTheDocument();
    expect(screen.getByText(/Not available with this box's own certificate/)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/PRIVATE KEY|private_key|key_pem/);
    expect(vi.mocked(api.httpsStatus)).toHaveBeenCalled();
  });
});
