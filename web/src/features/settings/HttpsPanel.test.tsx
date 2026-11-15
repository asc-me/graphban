import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpsPanel, detectOs, httpsAddress, probeTrust } from "@/features/settings/HttpsPanel";
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

  it("shows the loading state while the status is pending", async () => {
    vi.mocked(api.httpsStatus).mockReturnValue(new Promise(() => {}));
    mount();
    expect(screen.getByText("Loading HTTPS settings…")).toBeInTheDocument();
    expect(screen.queryByTestId("trust-chip")).toBeNull();
  });

  it("shows Checking… on the chip while the probe is pending", async () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
    mount();
    await screen.findByTestId("trust-chip");
    await userEvent.click(screen.getAllByRole("button", { name: "Check again" })[0]);
    expect(screen.getByTestId("trust-chip")).toHaveTextContent("Checking…");
    expect(screen.getByTestId("trust-chip")).not.toHaveTextContent("Trusted by this browser");
  });

  it("answers Can't tell when the probe times out", async () => {
    vi.useFakeTimers();
    try {
      // A fetch that never settles on its own; only the abort signal ends it.
      vi.stubGlobal("fetch", vi.fn((_u: string, init: RequestInit) => new Promise((_r, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      })));
      let result: string | undefined;
      const p = probeTrust("https://box.home", 5000).then((r) => { result = r; });
      await vi.advanceTimersByTimeAsync(4999);
      expect(result).toBeUndefined();
      await vi.advanceTimersByTimeAsync(1);
      await p;
      expect(result).toBe("unknown");
    } finally {
      vi.useRealTimers();
    }
  });

  it("puts the chip at Can't tell, not Trusted, when the page's probe times out", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.stubGlobal("fetch", vi.fn((_u: string, init: RequestInit) => new Promise((_r, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      })));
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      mount();
      await screen.findByTestId("trust-chip");
      await user.click(screen.getAllByRole("button", { name: "Check again" })[0]);
      expect(screen.getByTestId("trust-chip")).toHaveTextContent("Checking…");
      await vi.advanceTimersByTimeAsync(5000);
      await waitFor(() => expect(screen.getByTestId("trust-chip")).toHaveTextContent("Can't tell"));
      expect(screen.getByTestId("trust-chip")).not.toHaveTextContent("Trusted by this browser");
      expect(screen.getByText(/The check from this browser failed/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the refused block's keep-using-http line", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("cert")));
    mount();
    await screen.findByTestId("trust-chip");
    await userEvent.click(screen.getAllByRole("button", { name: "Check again" })[0]);
    const refused = await screen.findByTestId("trust-refused");
    expect(refused).toHaveTextContent("This browser still doesn't trust the box. Two ways out:");
    expect(refused).toHaveTextContent(`You can keep using http://${window.location.host} on this device.`);
    expect(screen.getByRole("link", { name: "Bring your own certificate" })).toHaveAttribute("href", "#bring-your-own");
    expect(screen.getByRole("link", { name: "Graphban Cloud" })).toHaveAttribute("href", "#graphban-cloud");
  });

  it("shows the Add-a-name help text", async () => {
    mount();
    expect(await screen.findByText(
      "Add any host name or address you reach this box by. The box finds the ones you already use automatically.",
    )).toBeInTheDocument();
  });

  it("shows the reissue message after adding a name", async () => {
    vi.mocked(api.httpsAddName).mockResolvedValue(status());
    mount();
    await userEvent.type(await screen.findByLabelText("Add a name"), "nas.lan");
    await userEvent.click(screen.getByRole("button", { name: "Add a name" }));
    expect(await screen.findByText(
      "The certificate was reissued with the new list. Devices that trust the box keep trusting it.",
    )).toBeInTheDocument();
    expect(vi.mocked(api.httpsAddName)).toHaveBeenCalledWith("nas.lan");
  });

  it("shows the reissue message after removing a name", async () => {
    vi.mocked(api.httpsRemoveName).mockResolvedValue(status());
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "Remove" }));
    expect(await screen.findByText(/The certificate was reissued with the new list/)).toBeInTheDocument();
    expect(vi.mocked(api.httpsRemoveName)).toHaveBeenCalledWith("box.home");
  });

  it("shows an add error as an alert, with no reissue message", async () => {
    vi.mocked(api.httpsAddName).mockRejectedValue(new Error("not a valid host name"));
    mount();
    await userEvent.type(await screen.findByLabelText("Add a name"), "bad name");
    await userEvent.click(screen.getByRole("button", { name: "Add a name" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("not a valid host name");
    expect(screen.queryByText(/The certificate was reissued/)).toBeNull();
  });

  it("shows a remove error as an alert", async () => {
    vi.mocked(api.httpsRemoveName).mockRejectedValue(new Error("remove refused"));
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "Remove" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("remove refused");
  });

  it("picks the OS tab from the browser platform", async () => {
    expect(detectOs("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")).toBe("iOS");
    expect(detectOs("Mozilla/5.0 (Linux; Android 14; Pixel 8)")).toBe("Android");
    expect(detectOs("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")).toBe("Windows");
    expect(detectOs("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)")).toBe("macOS");
    expect(detectOs("Mozilla/5.0 (X11; Linux x86_64)")).toBe("Linux");
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)");
    try {
      mount();
      expect(await screen.findByRole("tab", { name: "iOS" })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByText(/Certificate Trust Settings/)).toBeInTheDocument();
    } finally {
      vi.restoreAllMocks();
    }
  });
});
