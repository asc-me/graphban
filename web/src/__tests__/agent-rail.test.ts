import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { hasOwnAside, useAgentRail } from "@/components/shell/agentRail";

/**
 * The agent rail opened on every view, including the code graph, whose own 300px aside it
 * then sat beside. Arriving at the graph must leave it closed; the toggle still works there.
 */
describe("the agent rail on arrival", () => {
  it("knows which views carry their own aside, in both URL shapes", () => {
    // Self-host serves flat paths (`/code`); hosted nests them under the project tag.
    // The first version read only the hosted shape and missed every self-host graph.
    expect(hasOwnAside("/code")).toBe(true);
    expect(hasOwnAside("/code/backend/app.py")).toBe(true);
    expect(hasOwnAside("/tracker")).toBe(false);
    expect(hasOwnAside("/codex")).toBe(false);
    expect(hasOwnAside("/")).toBe(false);
    expect(hasOwnAside("/p/GRPH/code")).toBe(true);
    expect(hasOwnAside("/p/GRPH/code/backend/app.py")).toBe(true);
    expect(hasOwnAside("/p/GRPH/tracker")).toBe(false);
    expect(hasOwnAside("/p/GRPH/codex")).toBe(false);
    expect(hasOwnAside("/org/galaxy")).toBe(false);
  });

  it.each(["/code", "/p/GRPH/code"])("starts closed on %s from the first frame", (graph) => {
    // The FIRST render, not the settled one. Starting open and closing in an effect draws
    // the rail for a frame and then plays its 240ms close slide: the panel visibly opens
    // on its own, which is the complaint.
    const frames: boolean[] = [];
    renderHook(() => {
      const rail = useAgentRail(graph);
      frames.push(rail[0]);
      return rail;
    });
    expect(frames[0]).toBe(false);
    expect(frames.every((open) => !open)).toBe(true);
  });

  it.each(["/tracker", "/p/GRPH/tracker", "/p/GRPH/stack", "/"])(
    "is hidden by default on %s too, and the toggle opens it",
    (path) => {
      const frames: boolean[] = [];
      const { result } = renderHook(() => {
        const rail = useAgentRail(path);
        frames.push(rail[0]);
        return rail;
      });
      expect(frames.every((open) => !open)).toBe(true);
      act(() => result.current[1](true));
      expect(result.current[0]).toBe(true);
    },
  );

  it.each([
    ["/tracker", "/code"],
    ["/p/GRPH/tracker", "/p/GRPH/code"],
  ])("closes when you navigate from %s to %s with it open", (from, to) => {
    const { result, rerender } = renderHook(({ path }) => useAgentRail(path), {
      initialProps: { path: from },
    });
    act(() => result.current[1](true));
    expect(result.current[0]).toBe(true);
    rerender({ path: to });
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
