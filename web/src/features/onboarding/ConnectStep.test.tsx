/**
 * Quick-start ③ on its own (GRPH-1362): the paste route's 2-minute hints, and that no wait —
 * however long — turns the step ✓. Only the `mark` the server derived can do that.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

import { ConnectStep, GBAN_AGENTS, PairApproval } from "./ConnectStep";

vi.mock("@/lib/api", () => ({
  api: {
    approveCliPair: vi.fn(async (code: string) => {
      if (code === "BAD") throw new Error("no");
    }),
    createApiKey: vi.fn(async () => ({
      id: "k1", name: "k", prefix: "gb_sk_ab12", plaintext: "gb_sk_secret", scopes: [], revoked: false,
    })),
  },
}));

function renderStep(dedicated: boolean) {
  return render(
    <ConnectStep
      projectId="core"
      address={dedicated ? "https://gb.example.com" : ""}
      dedicated={dedicated}
      mark={{ status: "pending" }}
      agent="cursor"
      onAgent={() => undefined}
      route="key"
      onRoute={() => undefined}
      hintAfterMs={50}
    />,
  );
}

describe("ConnectStep", () => {
  it("after the wait it adds hints (curl on a dedicated server) and still never grants ✓", async () => {
    const user = userEvent.setup();
    renderStep(true);
    await user.click(screen.getByRole("button", { name: /Create a key for Cursor/ }));
    expect(await screen.findByText("Waiting for your agent's first call…")).toBeInTheDocument();
    expect(screen.queryByTestId("connect-hints")).not.toBeInTheDocument();
    expect(await screen.findByTestId("connect-hints")).toHaveTextContent("Restarted your agent?");
    expect(screen.getByText(/curl .*https:\/\/gb\.example\.com\/api\/mcp/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute("data-status", "pending");
  });

  it("this computer gets the restart hint without the curl check", async () => {
    const user = userEvent.setup();
    renderStep(false);
    await user.click(screen.getByRole("button", { name: /Create a key for Cursor/ }));
    await screen.findByTestId("connect-hints");
    expect(screen.queryByText(/curl /)).not.toBeInTheDocument();
  });

  it("the gban route list is the CLI's export", () => {
    expect(GBAN_AGENTS).toContain("claude");
    expect(GBAN_AGENTS).not.toContain("cursor");
  });
});

describe("PairApproval (GRPH-1366)", () => {
  function renderPair() {
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <PairApproval />
      </QueryClientProvider>,
    );
  }

  it("sends the typed code to the server and says so", async () => {
    const user = userEvent.setup();
    renderPair();
    await user.type(screen.getByLabelText(/Code shown by/), "ABCD-EFGH");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/signed in as you/)).toBeInTheDocument();
    expect(api.approveCliPair).toHaveBeenCalledWith("ABCD-EFGH");
  });

  it("an unknown or expired code shows an alert, not a success", async () => {
    const user = userEvent.setup();
    renderPair();
    await user.type(screen.getByLabelText(/Code shown by/), "BAD");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/isn't valid/);
  });
});
