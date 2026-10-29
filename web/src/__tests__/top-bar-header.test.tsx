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
});
