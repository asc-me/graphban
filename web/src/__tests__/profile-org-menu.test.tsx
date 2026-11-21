import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { TopBar } from "@/components/shell/TopBar";
import { ProfileView } from "@/features/profile/ProfileView";

const user = { id: "u1", name: "Ada Lovelace", handle: "ada", email: "ada@x.io", avatar: "#c6f24e", initials: "AL" };

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({ user, setUser: vi.fn(), logout: vi.fn() }),
}));
vi.mock("@/components/shell/AlertsBell", () => ({ AlertsBell: () => null }));
vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    apiKeys: vi.fn(async () => []),
    mcpTools: vi.fn(async () => ({ live: 1, tools: [] })),
    config: vi.fn(async () => ({ hosted_mode: false })),
    myMemberships: vi.fn(async () => []),
    syncStatus: vi.fn(async () => ({ linked: false, projects: [] })),
  },
}));

describe("account menu → Organizations", () => {
  it("lands on the profile and scrolls the Organizations section into view", async () => {
    const scrolled: Element[] = [];
    const spy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(function (this: Element) {
      scrolled.push(this);
    });
    const u = userEvent.setup();
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/"]}>
          <TopBar agentOpen={false} onToggleAgent={() => {}} onOpenPalette={() => {}} />
          <Routes>
            <Route path="/" element={<div>home</div>} />
            <Route path="/profile" element={<ProfileView />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await u.click(screen.getByRole("button", { name: "Ada Lovelace" }));
    await u.click(await screen.findByRole("menuitem", { name: /Organizations/ }));
    const section = await screen.findByRole("region", { name: "Organizations" });
    expect(section.id).toBe("organizations");
    expect(await screen.findByText("Link this box")).toBeTruthy();
    await waitFor(() => expect(scrolled).toContain(section));
    spy.mockRestore();
  });
});
