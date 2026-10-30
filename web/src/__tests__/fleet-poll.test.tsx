import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { useFleet } from "@/lib/queries";

/** GRPH-1229: a 10s roster polled every 15s fired six requests in 15s on the walk. */
describe("fleet poll", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

  it("does not refetch while a request is pending", async () => {
    let resolve!: (v: unknown) => void;
    const spy = vi.spyOn(api, "fleet").mockImplementation(
      () => new Promise((r) => { resolve = r; }) as never);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    renderHook(() => useFleet("core"), { wrapper });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(spy).toHaveBeenCalledTimes(1);

    resolve({ agents: [] });
    await vi.advanceTimersByTimeAsync(15_100);
    expect(spy).toHaveBeenCalledTimes(2);
  });
});
