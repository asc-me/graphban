import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { useFleet } from "@/lib/queries";

/**
 * GRPH-1229: a 10s roster was read six times in 15s on the walk. The interval was not the
 * cause — React Query restarts it on every fetch and joins a pending read. `refetch()` and
 * `invalidateQueries` were: both cancel the pending read and start another, and the server
 * never sees the cancel, so each one stacked a full read on it.
 */
describe("fleet reads never overlap", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  function slowFleet() {
    const pending: ((v: unknown) => void)[] = [];
    const spy = vi.spyOn(api, "fleet").mockImplementation(
      () => new Promise((r) => { pending.push(r); }) as never);
    return { spy, resolveNext: () => pending.shift()!({ agents: [] }) };
  }

  function mount(projectId: string) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    return { qc, hook: renderHook(() => useFleet(projectId), { wrapper }) };
  }

  const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

  /** Mounted, first roster in hand, and a second read pending — the state the walk was in. */
  async function polling(projectId: string) {
    const fleet = slowFleet();
    const m = mount(projectId);
    fleet.resolveNext();
    await flush();
    act(() => { void m.hook.result.current.refetch(); });
    await flush();
    expect(fleet.spy).toHaveBeenCalledTimes(2);
    return { ...fleet, ...m };
  }

  it("a refetch while a read is pending waits for it, then reads once", async () => {
    const { spy, resolveNext, hook } = await polling("p-refetch");

    act(() => { void hook.result.current.refetch(); });
    await flush();
    expect(spy).toHaveBeenCalledTimes(2);

    resolveNext();
    await flush();
    expect(spy).toHaveBeenCalledTimes(3);
    resolveNext();
    await flush();
    expect(spy).toHaveBeenCalledTimes(3);
  });

  it("a burst of invalidations while a read is pending queues ONE read, not one each", async () => {
    const { spy, resolveNext, qc } = await polling("p-burst");

    for (let i = 0; i < 5; i++) void qc.invalidateQueries({ queryKey: ["fleet", "p-burst"] });
    await flush();
    expect(spy).toHaveBeenCalledTimes(2);

    resolveNext();
    await flush();
    resolveNext();
    await flush();
    expect(spy).toHaveBeenCalledTimes(3);
  });
});
