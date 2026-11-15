import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SecureAddressNote } from "@/features/home/SetupStatusCard";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({ api: { httpsStatus: vi.fn() } }));
const ready = { tier: "box-ca", ready: true, names: [{ name: "box", source: "auto", origin: "" }] };

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><SecureAddressNote /></MemoryRouter></QueryClientProvider>);
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.httpsStatus).mockResolvedValue(ready as never);
  vi.stubGlobal("fetch", vi.fn(async () => ({})));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

describe("SecureAddressNote", () => {
  it("shows the secure-address line on http with an HTTPS address", async () => {
    mount();
    const note = await screen.findByTestId("secure-address-note");
    expect(note).toHaveTextContent("Use the secure address for browser notifications: https://");
    expect(screen.getByRole("link", { name: "HTTPS settings" })).toHaveAttribute("href", "/settings/deployment/https");
  });

  it("shows the untrusted variant with the address as plain text when the probe fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("cert"); }));
    mount();
    const note = await screen.findByTestId("secure-address-note");
    expect(note).toHaveTextContent("Trust this box on this device to use https://");
    expect(note.querySelectorAll("a")).toHaveLength(1);
  });

  it("is absent without an HTTPS address", async () => {
    vi.mocked(api.httpsStatus).mockResolvedValue({ ...ready, ready: false } as never);
    mount();
    await waitFor(() => expect(api.httpsStatus).toHaveBeenCalled());
    expect(screen.queryByTestId("secure-address-note")).toBeNull();
  });

  it("stays dismissed after reload", async () => {
    const first = mount();
    await userEvent.click(await screen.findByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("secure-address-note")).toBeNull();
    first.unmount();
    mount();
    await waitFor(() => expect(api.httpsStatus).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("secure-address-note")).toBeNull();
  });
});
