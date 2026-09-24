import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { PublicBoardPage } from "@/features/public/PublicBoardPage";
import { PublicTrackingPage } from "@/features/public/PublicTrackingPage";

function renderWithRoute(path: string, element: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>{element}</Routes>
    </MemoryRouter>,
  );
}

describe("Public pages (GRPH-901 sabotage)", () => {
  it("issues board shows 404 when flag is off (not empty state)", async () => {
    global.fetch = vi.fn().mockResolvedValue({ status: 404, ok: false });
    renderWithRoute(
      "/public/test-token/issues",
      <Route path="/public/:token/issues" element={<PublicBoardPage kind="issues" />} />,
    );
    const heading = await screen.findByText(/not found/i);
    expect(heading).toBeTruthy();
    expect(screen.queryByText(/no published/i)).toBeNull();
  });

  it("issues board shows empty state when flag is on but no rows", async () => {
    global.fetch = vi.fn().mockResolvedValue({ status: 200, ok: true, json: async () => [] });
    renderWithRoute(
      "/public/test-token/issues",
      <Route path="/public/:token/issues" element={<PublicBoardPage kind="issues" />} />,
    );
    const heading = await screen.findByText(/no published issues/i);
    expect(heading).toBeTruthy();
    expect(screen.queryByText(/not found/i)).toBeNull();
  });

  it("tracking page shows 404 for unknown token", async () => {
    global.fetch = vi.fn().mockResolvedValue({ status: 404, ok: false });
    renderWithRoute(
      "/track/bad-token",
      <Route path="/track/:trackToken" element={<PublicTrackingPage />} />,
    );
    const heading = await screen.findByText(/not found/i);
    expect(heading).toBeTruthy();
  });

  it("tracking page renders data for a valid token", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({
        title: "My request",
        type: "bug",
        status: "new",
        votes: 5,
        comments: [{ id: "c1", body: "Thanks!", created_at: "2026-01-01T00:00:00Z" }],
      }),
    });
    renderWithRoute(
      "/track/good-token",
      <Route path="/track/:trackToken" element={<PublicTrackingPage />} />,
    );
    const title = await screen.findByText("My request");
    expect(title).toBeTruthy();
    expect(screen.getByText("Thanks!")).toBeTruthy();
  });
});
