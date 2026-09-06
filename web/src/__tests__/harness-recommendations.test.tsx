import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { Recommendations } from "@/features/harness/Recommendations";
import { ProjectProvider } from "@/features/ProjectContext";
import type { HarnessCard, HarnessRecommendations } from "@/lib/types";

function key(over = {}) {
  return {
    vendor: "gbagent", model: "qwen3.6", binary_version: "1.0.0", lane: "backend",
    tier: "cheap", task_class: "general", size_band: "L", ...over,
  };
}

function card(over: Partial<HarnessCard> = {}): HarnessCard {
  return {
    rule: "R1", key: "R1:gbagent:qwen3.6:1.0.0", evidence_hash: "abc123",
    title: "promote gbagent:qwen3.6 for backend",
    detail: "signed off 10 of 10 in backend/general/L and its matrix row is still unverified.",
    cells: [{ cell: key(), finished: 10, signed_off: 10, rate: 1 }],
    siblings: [{ cell: key({ size_band: "S" }), finished: 9, signed_off: 2, rate: 0.222 }],
    draft: { target: "gbagent:qwen3.6", where: "fleet/src/gbfleet/matrix.toml",
             evidence_line: "verified: 10/10 signed off in backend/general/L" },
    replay: { considered: 19, changed: 4, skipped_no_resolution: 2, truncated: false,
              moves: [{ from: "claude:sonnet", to: "gbagent:qwen3.6", count: 4 }],
              summary: "would have changed 4 of 19 recorded resolutions; 2 had no recorded resolution to replay" },
    thresholds: { min_finished: 10, min_rate: 0.8 },
    state: "new", previously: null, ...over,
  };
}

function payload(over: Partial<HarnessRecommendations> = {}): HarnessRecommendations {
  return { project_id: "core", cards: [card()], rules: ["R1", "R2", "R3", "R4"],
           lessons_drafted: [], window_days: 90, floor: 5, ...over };
}

const cards = vi.fn(async () => payload());
const mark = vi.fn(async () => ({ card_key: "R1:gbagent:qwen3.6:1.0.0", state: "accepted",
                                  evidence_hash: "abc123", lesson_drafted: "m_1" }));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", tag: "GRPH" }]),
    harnessRecommendations: (...a: unknown[]) => cards(...(a as [])),
    markHarnessRecommendation: (...a: unknown[]) => mark(...(a as [])),
  },
}));

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/harness"]}>
        <ProjectProvider>
          <Recommendations projectId="core" />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Harness recommendations", () => {
  it("names the rule and shows the replay rather than a bare suggestion", async () => {
    show();
    const row = await screen.findByTestId("harness-card");
    expect(row).toHaveAttribute("data-rule", "R1");
    expect(screen.getByTestId("harness-card-replay")).toHaveTextContent(
      "would have changed 4 of 19 recorded resolutions",
    );
    // The replay must not hide what it could not speak for.
    expect(screen.getByTestId("harness-card-replay")).toHaveTextContent(
      "2 had no recorded resolution",
    );
  });

  it("shows the sibling cells it did not fire on, and says that is what accepting generalises over", async () => {
    show();
    fireEvent.click(await screen.findByTestId("harness-card-expand"));
    const siblings = screen.getByTestId("harness-card-siblings");
    expect(siblings).toHaveTextContent("generalises over");
    expect(within(siblings).getByText(/backend\/general\/S · 2\/9/)).toBeInTheDocument();
  });

  it("says the page applies nothing and points at where the change is made", async () => {
    show();
    fireEvent.click(await screen.findByTestId("harness-card-expand"));
    const apply = screen.getByTestId("harness-card-apply");
    expect(apply).toHaveTextContent("this page changes nothing");
    expect(apply).toHaveTextContent("fleet/src/gbfleet/matrix.toml");
    expect(apply).toHaveTextContent("verified: 10/10 signed off in backend/general/L");
  });

  it("marks a card with its evidence hash so the mark expires when the numbers move", async () => {
    show();
    fireEvent.click(await screen.findByTestId("harness-accept"));
    await waitFor(() =>
      expect(mark).toHaveBeenCalledWith({
        project_id: "core", card_key: "R1:gbagent:qwen3.6:1.0.0",
        evidence_hash: "abc123", action: "accept",
      }),
    );
  });

  it("tells a person when a card they already acted on has come back", async () => {
    cards.mockResolvedValueOnce(payload({
      cards: [card({ previously: { state: "accepted", at: "2026-09-01T00:00:00Z",
                                   evidence_changed: true } })],
    }));
    show();
    expect(await screen.findByTestId("harness-card-returned")).toHaveTextContent(
      "the evidence has changed since",
    );
  });

  it("says why there is nothing to recommend instead of rendering blank", async () => {
    cards.mockResolvedValueOnce(payload({ cards: [] }));
    show();
    expect(await screen.findByTestId("harness-no-cards")).toHaveTextContent(
      "The four rules fire on cells above the 5-attempt floor",
    );
  });
});
