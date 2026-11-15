import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BringYourOwn, LetsEncrypt, RedirectSwitch, refusals } from "@/features/settings/https/CertificateSections";
import { api } from "@/lib/api";
import type { HttpsCustom } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: { httpsUpload: vi.fn(), httpsLetsEncrypt: vi.fn(), httpsStopUsing: vi.fn(), httpsRedirect: vi.fn() },
}));

const c = (over: Partial<HttpsCustom> = {}): HttpsCustom => ({
  kind: "upload", state: "active", box_address: "graphban.acme.com", subject: "graphban.acme.com",
  not_after: "2027-03-01T00:00:00+00:00", warnings: [], reason: "", redirect_on: true, ...over,
});

function mount(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}
afterEach(() => vi.clearAllMocks());

describe("BringYourOwn", () => {
  it("checks and uses, then shows each refusal reason", async () => {
    vi.mocked(api.httpsUpload).mockRejectedValue(new Error(JSON.stringify({ detail: { reasons: [
      { code: "key_mismatch", message: "the key does not match the certificate" },
      { code: "expired", message: "the certificate expired on 2025-01-01" },
    ] } })));
    mount(<BringYourOwn custom={null} />);
    await userEvent.type(screen.getByLabelText("Certificate chain"), "CERT");
    await userEvent.type(screen.getByLabelText("Private key"), "KEY");
    await userEvent.type(screen.getByLabelText("Box address"), "graphban.acme.com");
    await userEvent.click(screen.getByRole("button", { name: "Check and use" }));
    const list = await screen.findByTestId("refused");
    expect(list).toHaveTextContent("the key does not match");
    expect(list).toHaveTextContent("expired on 2025-01-01");
    expect(api.httpsUpload).toHaveBeenCalledWith("CERT", "KEY", "graphban.acme.com");
  });

  it("serving, with wildcard and address-not-here warnings stacked", () => {
    mount(<BringYourOwn custom={c({ warnings: ["wildcard", "address_not_here"] })} />);
    expect(screen.getByTestId("serving")).toHaveTextContent("Serving your certificate");
    expect(screen.getByTestId("warn-wildcard")).toBeInTheDocument();
    expect(screen.getByTestId("warn-address")).toBeInTheDocument();
  });

  it("expiring soon", () => {
    mount(<BringYourOwn custom={c({ state: "expiring-soon" })} />);
    expect(screen.getByTestId("expiring-soon")).toHaveTextContent("Expiring soon");
  });

  it("expired marks the old file EXPIRED and says why", () => {
    mount(<BringYourOwn custom={c({ state: "expired", reason: "your certificate expired on 2026-01-01" })} />);
    expect(screen.getByTestId("expired")).toHaveTextContent("Your certificate expired on 2026-01-01");
    expect(screen.getByText("EXPIRED")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check and use" })).toBeInTheDocument();
  });

  it("stop asks inline, then reports done", async () => {
    vi.mocked(api.httpsStopUsing).mockResolvedValue({} as never);
    mount(<BringYourOwn custom={c()} />);
    await userEvent.click(screen.getByRole("button", { name: "Stop using this certificate" }));
    expect(api.httpsStopUsing).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Yes, stop using it" }));
    await waitFor(() => expect(api.httpsStopUsing).toHaveBeenCalled());
  });
});

describe("LetsEncrypt", () => {
  it("renders pending, failed with reason, and active", () => {
    const { unmount } = mount(<LetsEncrypt custom={c({ kind: "letsencrypt", state: "pending" })} />);
    expect(screen.getByTestId("le-pending")).toBeInTheDocument();
    unmount();
    const r = mount(<LetsEncrypt custom={c({ kind: "letsencrypt", state: "failed", reason: "could not reach x" })} />);
    expect(screen.getByTestId("le-failed")).toHaveTextContent("could not reach x");
    expect(screen.getByTestId("le-failed")).toHaveTextContent("still serving its current certificate");
    r.unmount();
    mount(<LetsEncrypt custom={c({ kind: "letsencrypt" })} />);
    expect(screen.getByTestId("le-active")).toHaveTextContent("renews itself");
    expect(screen.getByRole("button", { name: "Stop using Let's Encrypt" })).toBeInTheDocument();
  });

  it("requests a domain", async () => {
    vi.mocked(api.httpsLetsEncrypt).mockResolvedValue({} as never);
    mount(<LetsEncrypt custom={null} />);
    await userEvent.type(screen.getByLabelText("Public domain"), "graphban.acme.com");
    await userEvent.click(screen.getByRole("button", { name: "Get a certificate" }));
    await waitFor(() => expect(api.httpsLetsEncrypt).toHaveBeenCalledWith("graphban.acme.com"));
  });
});

describe("RedirectSwitch", () => {
  it("is unavailable with the box's own certificate", () => {
    mount(<RedirectSwitch custom={null} />);
    expect(screen.getByText(/Not available with this box's own certificate/)).toBeInTheDocument();
    expect(screen.queryByRole("switch")).toBeNull();
  });
  it("on names the box address; off says http is served; toggling calls the API", async () => {
    vi.mocked(api.httpsRedirect).mockResolvedValue({} as never);
    const { unmount } = mount(<RedirectSwitch custom={c()} />);
    expect(screen.getByTestId("redirect-copy")).toHaveTextContent("https://graphban.acme.com/");
    await userEvent.click(screen.getByRole("switch"));
    await waitFor(() => expect(api.httpsRedirect).toHaveBeenCalledWith(false));
    unmount();
    mount(<RedirectSwitch custom={c({ redirect_on: false })} />);
    expect(screen.getByTestId("redirect-copy")).toHaveTextContent("served as they are");
  });
});

describe("refusals", () => {
  it("falls back to the plain message", () => {
    expect(refusals(new Error("boom"))[0].message).toBe("boom");
  });
});
