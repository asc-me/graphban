import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { TopBar } from "@/components/shell/TopBar";

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({ user: null, logout: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    apiKeys: vi.fn(async () => []),
    mcpTools: vi.fn(async () => ({ live: 7, tools: [] })),
  },
}));

function renderBar() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <TopBar agentOpen={false} onToggleAgent={() => {}} onOpenPalette={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TopBar (GRPH-1174)", () => {
  it("renders the brand subline as mono uppercase", () => {
    renderBar();
    const sub = screen.getByText("Agent memory · linear execution");
    expect(sub.className).toMatch(/font-mono/);
    expect(sub.className).toMatch(/uppercase/);
    expect(sub.className).toMatch(/text-meta/);
  });

  it("renders the MCP pill with a blinking dot", async () => {
    renderBar();
    const pill = await screen.findByText(/MCP · 7 tools live/);
    expect(pill.className).toMatch(/font-mono/);
    expect(pill.className).toMatch(/uppercase/);
    expect(pill.querySelector(".blink")).toBeInTheDocument();
  });

  it("a live count, then the API goes down: the chip re-reads and says unavailable, with the reason (GRPH-1547)", async () => {
    const { api } = await import("@/lib/api");
    const { mcpToolsTiming } = await import("@/lib/queries");
    const saved = mcpToolsTiming.pollMs;
    mcpToolsTiming.pollMs = 30;
    try {
      vi.mocked(api.mcpTools).mockResolvedValue({ live: 78, tools: [] });
      renderBar();
      expect(await screen.findByText(/MCP · 78 tools live/)).toBeInTheDocument();
      vi.mocked(api.mcpTools).mockRejectedValue(new Error("the API is unreachable (502 from the proxy)"));
      const chip = await screen.findByText(/MCP · unavailable/, {}, { timeout: 2000 });
      expect(chip).toHaveAttribute("title", "the API is unreachable (502 from the proxy)");
      expect(chip.querySelector(".blink")).toBeNull();
      expect(screen.queryByText(/tools live/)).toBeNull();
    } finally {
      mcpToolsTiming.pollMs = saved;
    }
  });
});
