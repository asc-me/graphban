import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AlertsBell } from "@/components/shell/AlertsBell";

const state: { feed: unknown; fail: boolean } = { feed: null, fail: false };
vi.mock("@/lib/api", () => ({
  api: {
    myAlerts: async () => {
      if (state.fail) throw new Error("boom");
      return state.feed;
    },
    readAlert: async () => state.feed,
    projects: async () => [{ id: "prj_other", tag: "OTH" }],
    config: async () => ({ hosted_mode: true }),
  },
}));

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AlertsBell />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AlertsBell", () => {
  beforeEach(() => {
    state.fail = false;
  });
  afterEach(() => cleanup());

  it("shows the unread count and lists alerts", async () => {
    state.feed = {
      unread: 2,
      alerts: [
        { id: "al_1", kind: "response_needed", project: "prj_other", question: "Which store?", body: "", wait: "GRPH-2",
          ticket: "GRPH-1", prd: "GRPH-P75", asked_by: "GRPH-A3", created_at: null, read: false },
      ],
    };
    mount();
    await waitFor(() => expect(screen.getByTestId("alerts-count")).toHaveTextContent("2"));
    await userEvent.click(screen.getByRole("button", { name: /alerts/i }));
    expect(screen.getByText("Which store?")).toBeInTheDocument();
    // The link is to the ALERT's project's Triage row, not whichever project is open.
    await waitFor(() =>
      expect(screen.getByText("Which store?").closest("a")).toHaveAttribute("href", "/p/OTH/triage?item=GRPH-1"),
    );
  });

  it("a failed read says could not load alerts and never renders 0", async () => {
    state.fail = true;
    mount();
    await userEvent.click(await screen.findByRole("button", { name: /could not load alerts/i }));
    expect(await screen.findByText(/could not load alerts/i)).toBeInTheDocument();
    expect(screen.getByTestId("alerts-count")).not.toHaveTextContent("0");
  });
});
