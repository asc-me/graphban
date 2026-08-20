import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

/**
 * PRD-21 D9 — designed, then hidden until ready.
 *
 * The gate was applied to the nav item and not to the route, so `/org/admin/branding`
 * rendered in a normal build for anyone who typed or was sent the URL. A nav gate on its
 * own is worse than none: it says somebody intended this hidden, which makes the
 * reachable route read as an oversight rather than a decision, and the next person to
 * find it cannot tell which it was.
 *
 * So these assert on the ROUTE, by navigating to it. Checking the nav would pass on
 * exactly the code that shipped the bug.
 */

/** The route tree under test, matching how App.tsx composes the admin shell. */
function AdminRoutes({ speculative }: { speculative: boolean }) {
  return (
    <Routes>
      <Route path="/org/admin">
        <Route path="users" element={<div>USERS SCREEN</div>} />
        {speculative && <Route path="branding" element={<div>BRANDING SCREEN</div>} />}
      </Route>
      <Route path="*" element={<div>NOT FOUND</div>} />
    </Routes>
  );
}

describe("the speculative gate covers the route, not only the nav", () => {
  it("does not render a speculative screen reached by URL in a normal build", () => {
    render(
      <MemoryRouter initialEntries={["/org/admin/branding"]}>
        <AdminRoutes speculative={false} />
      </MemoryRouter>,
    );
    expect(screen.queryByText("BRANDING SCREEN")).toBeNull();
    expect(screen.getByText("NOT FOUND")).toBeTruthy();
  });

  it("renders it when the flag is on, so the test proves the gate and not a missing component", () => {
    render(
      <MemoryRouter initialEntries={["/org/admin/branding"]}>
        <AdminRoutes speculative={true} />
      </MemoryRouter>,
    );
    expect(screen.getByText("BRANDING SCREEN")).toBeTruthy();
  });

  it("leaves backed screens reachable either way", () => {
    for (const speculative of [false, true]) {
      const { unmount } = render(
        <MemoryRouter initialEntries={["/org/admin/users"]}>
          <AdminRoutes speculative={speculative} />
        </MemoryRouter>,
      );
      expect(screen.getByText("USERS SCREEN")).toBeTruthy();
      unmount();
    }
  });
});

/**
 * The real App.tsx wiring, asserted against the source rather than the render tree.
 *
 * The component test above proves the SHAPE is right; this proves App.tsx actually uses
 * it. Without this, App.tsx could drop the guard tomorrow and the tests above would still
 * be green — they would be testing a route tree that no longer resembles the app.
 */
describe("App.tsx wires the gate it is supposed to", () => {
  it("guards every speculative route with the flag", () => {
    const sources = import.meta.glob("../App.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0];
    expect(src, "App.tsx must be readable for this assertion to mean anything").toBeTruthy();

    const at = src.indexOf('path="branding"');
    expect(at, "the branding route must still exist, or this test is asserting nothing").toBeGreaterThan(0);
    expect(src.slice(Math.max(0, at - 400), at)).toContain("SPECULATIVE_ENABLED");
  });
});
