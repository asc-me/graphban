import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConsoleRedirect } from "@/components/ConsoleRedirect";
import { consoleHref, consolePathFromProduct } from "@/lib/routes";

describe("consolePathFromProduct", () => {
  it("maps operator bookmarks onto /operator", () => {
    expect(consolePathFromProduct("/admin")).toBe("/operator");
    expect(consolePathFromProduct("/admin/users")).toBe("/operator/users");
  });

  it("keeps org-plane paths unchanged", () => {
    expect(consolePathFromProduct("/org")).toBe("/org");
    expect(consolePathFromProduct("/org/admin/billing")).toBe("/org/admin/billing");
  });
});

describe("ConsoleRedirect", () => {
  const replace = vi.fn();

  beforeEach(() => {
    replace.mockReset();
    vi.stubEnv("VITE_CONSOLE_URL", "http://console.test");
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, replace },
    });
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  function renderAt(path: string) {
    return render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/admin/*" element={<ConsoleRedirect />} />
          <Route path="/org/*" element={<ConsoleRedirect />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("sends /admin/users to the console operator path", () => {
    renderAt("/admin/users");
    expect(replace).toHaveBeenCalledWith(
      `${consoleHref("/admin/users")}`,
    );
    expect(replace.mock.calls[0][0]).toMatch(/\/operator\/users$/);
  });

  it("sends /org/admin/billing to the same org path on the console", () => {
    renderAt("/org/admin/billing");
    expect(replace).toHaveBeenCalledWith(consoleHref("/org/admin/billing"));
    expect(replace.mock.calls[0][0]).toMatch(/\/org\/admin\/billing$/);
  });
});
