import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ActivityView } from "@/features/activity/ActivityView";
import { MemoryRouter } from "react-router-dom";

import { ProjectProvider } from "@/features/ProjectContext";
import type { EventPage } from "@/lib/types";

const page: EventPage = {
  results: [
    {
      id: 2, ts: new Date().toISOString(), actor_type: "apikey", actor_id: "k1",
      actor_label: "loop-agent", principal: "alex", agent: "loop-agent",
      surface: "mcp", action: "create_item",
      target_type: "item", target_id: "AL-42", project_id: "core", meta: null,
    },
    {
      id: 1, ts: new Date().toISOString(), actor_type: "user", actor_id: "u1",
      actor_label: "ascme", surface: "rest", action: "revoke_api_key",
      target_type: "api_key", target_id: "k9", project_id: "core", meta: { name: "old" },
    },
  ],
  total: 2, limit: 100, offset: 0, has_more: false,
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => []),
    events: vi.fn(async () => page),
  },
}));

describe("Activity ledger", () => {
  it("renders events with actor, action, and target", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/activity"]}>
          <ProjectProvider>
            <ActivityView />
          </ProjectProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // AL-197: the agent action shows the human principal AND the agent ("alex via loop-agent")
    expect(await screen.findByText("alex")).toBeInTheDocument();
    expect(screen.getByText("loop-agent")).toBeInTheDocument();
    expect(screen.getByText("via")).toBeInTheDocument();
    expect(screen.getByText("create_item")).toBeInTheDocument();
    expect(screen.getByText("AL-42")).toBeInTheDocument();
    expect(screen.getByText("revoke_api_key")).toBeInTheDocument();
  });
});
