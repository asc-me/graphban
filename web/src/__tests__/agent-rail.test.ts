import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { hasOwnAside, useAgentRail } from "@/components/shell/agentRail";

/**
 * The agent rail opened on every view, including the code graph, whose own 300px aside it
 * then sat beside. Arriving at the graph must leave it closed; the toggle still works there.
 */
describe("the agent rail on arrival", () => {
  it("knows which views carry their own aside", () => {
    expect(hasOwnAside("/p/GRPH/code")).toBe(true);
    expect(hasOwnAside("/p/GRPH/code/backend/app.py")).toBe(true);
    expect(hasOwnAside("/p/GRPH/tracker")).toBe(false);
    expect(hasOwnAside("/p/GRPH/codex")).toBe(false);
    expect(hasOwnAside("/org/galaxy")).toBe(false);
  });

  it("starts open on an ordinary view and closed on the code graph, from the first frame", () => {
    // The FIRST render, not the settled one. Starting open and closing in an effect draws
    // the rail for a frame and then plays its 240ms close slide: the panel visibly opens
    // on its own, which is the complaint.
    const frames: boolean[] = [];
    renderHook(() => {
      const rail = useAgentRail("/p/GRPH/code");
      frames.push(rail[0]);
      return rail;
    });
    expect(frames[0]).toBe(false);
    expect(frames.every((open) => !open)).toBe(true);

    expect(renderHook(() => useAgentRail("/p/GRPH/tracker")).result.current[0]).toBe(true);
  });

  it("closes when you navigate to the code graph with it open", () => {
    const { result, rerender } = renderHook(({ path }) => useAgentRail(path), {
      initialProps: { path: "/p/GRPH/tracker" },
    });
    expect(result.current[0]).toBe(true);
    rerender({ path: "/p/GRPH/code" });
    expect(result.current[0]).toBe(false);
  });

  it("still opens from the toggle on the code graph, and stays open while you are there", () => {
    const { result, rerender } = renderHook(({ path }) => useAgentRail(path), {
      initialProps: { path: "/p/GRPH/code" },
    });
    act(() => result.current[1](true));
    expect(result.current[0]).toBe(true);
    rerender({ path: "/p/GRPH/code/backend/app.py" });
    expect(result.current[0]).toBe(true);
  });

  it("leaves the user's choice alone when leaving the graph", () => {
    const { result, rerender } = renderHook(({ path }) => useAgentRail(path), {
      initialProps: { path: "/p/GRPH/code" },
    });
    rerender({ path: "/p/GRPH/tracker" });
    expect(result.current[0]).toBe(false);
  });
});
