import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { FleetPage } from "@/features/fleet/FleetPage";

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ active: { id: "core", name: "Core", tag: "core" }, activeId: "core", projects: [] }),
}));
vi.mock("@/lib/queries", () => ({
  useFleet: () => ({ data: { total: 3, online: 2, matrix: { rows: [{ harness: "claude" }] } } }),
}));
// The bodies are covered by fleet-view / fleet-v2; this file is about the page around them.
vi.mock("@/features/fleet/FleetView", async () => {
  const { FleetPageTabs: Tabs } = await import("@/features/fleet/FleetPageTabs");
  return { FleetView: () => (<div data-testid="fleet-v1"><Tabs active="manage" /></div>) };
});
vi.mock("@/features/fleet/FleetV2View", async () => {
  const { FleetPageTabs: Tabs } = await import("@/features/fleet/FleetPageTabs");
  return { FleetV2View: () => (<div data-testid="fleet-v2"><Tabs active="harness" /></div>) };
});

function Where() {
  const { search } = useLocation();
  return <span data-testid="search">{search}</span>;
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/fleet" element={<><FleetPage /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Fleet page tabs", () => {
  it("defaults to Fleet management and keeps meta counts", () => {
    renderAt("/fleet");
    expect(screen.getByTestId("fleet-v1")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Fleet management/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Fleet management/ })).toHaveTextContent("2 of 3 online");
    expect(screen.getByRole("tab", { name: /Harness config/ })).toHaveTextContent("1 in catalog");
  });

  it("reads the tab from the URL, so a reload keeps it", () => {
    renderAt("/fleet?tab=harness");
    expect(screen.getByTestId("fleet-v2")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Harness config/ })).toHaveAttribute("aria-selected", "true");
  });

  it("clicking a tab writes it to the URL and swaps the body", () => {
    renderAt("/fleet");
    fireEvent.click(screen.getByRole("tab", { name: /Harness config/ }));
    expect(screen.getByTestId("search")).toHaveTextContent("?tab=harness");
    expect(screen.getByTestId("fleet-v2")).toBeInTheDocument();
  });

  it("arrow keys move between tabs", () => {
    renderAt("/fleet");
    fireEvent.keyDown(screen.getByRole("tab", { name: /Fleet management/ }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: /Harness config/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Harness config/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("tab", { name: /Harness config/ }), { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: /Fleet management/ })).toHaveAttribute("aria-selected", "true");
  });
});

describe("old fleet routes", () => {
  it("no source links to fleet.v1 or fleet.v2 except the redirects", () => {
    const sources = import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    const offenders = Object.entries(sources)
      .filter(([path]) => !/__tests__|\.test\./.test(path) && !path.endsWith("/App.tsx"))
      .filter(([, src]) => /fleet\.v[12]/.test(src))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});

