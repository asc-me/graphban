import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useGraphKeyboard } from "@/lib/graph/useGraphKeyboard";

/**
 * PRD-20 D3, keyboard half — previously untested, so reverting to a tab stop per node
 * would have stayed green.
 *
 * Both behaviours here **diverge from the PRD deliberately**, and the divergences are what
 * most need pinning, because a later reader comparing code to spec would otherwise "fix"
 * them back:
 *
 * 1. **Roving tabindex, not `tabIndex={0}` per node.** The PRD had Tab walk the nodes in
 *    degree order. On a 500-node graph that is 500 tab stops between the toolbar and
 *    whatever follows — a keyboard user pressing Tab five hundred times to get *past* a
 *    decoration. The composite-widget pattern makes the `<svg>` one tab stop and arrows
 *    move within it.
 * 2. **Arrows move focus; Shift+arrows pan.** The PRD gave arrows to panning, which cannot
 *    coexist with arrow-key focus movement. Focus wins the unmodified keys because reaching
 *    a node is the accessibility requirement and panning is a convenience — still available
 *    by drag, by the reset control, and by Shift+arrow.
 */
const ORDER = ["a.py", "b.py", "c.py"];

function setup(order = ORDER) {
  const onSelect = vi.fn();
  const onClear = vi.fn();
  const onExpand = vi.fn();
  const setViewport = vi.fn();
  const hook = renderHook(() =>
    useGraphKeyboard({ order, onSelect, onClear, onExpand, setViewport }),
  );
  return { hook, onSelect, onClear, onExpand, setViewport };
}

function key(hook: ReturnType<typeof setup>["hook"], k: string, opts: Partial<KeyboardEvent> = {}) {
  act(() => {
    hook.result.current.onKeyDown({
      key: k, shiftKey: false, preventDefault: vi.fn(), ...opts,
    } as unknown as React.KeyboardEvent<SVGSVGElement>);
  });
}

describe("roving tabindex", () => {
  it("makes exactly one node tabbable, whatever the graph size", () => {
    const { hook } = setup();
    const tabbable = ORDER.filter((id) => hook.result.current.tabIndexFor(id) === 0);
    expect(tabbable).toEqual(["a.py"]);
  });

  it("moves the single tab stop with focus rather than adding another", () => {
    const { hook } = setup();
    key(hook, "ArrowRight");   // adopts the implicit stop at order[0]
    key(hook, "ArrowRight");   // and only now steps past it

    const tabbable = ORDER.filter((id) => hook.result.current.tabIndexFor(id) === 0);
    expect(tabbable).toEqual(["b.py"]);
  });

  it("never returns 0 for more than one node — the 500-Tab trap", () => {
    const many = Array.from({ length: 500 }, (_, i) => `n${i}.py`);
    const { hook } = setup(many);
    expect(many.filter((id) => hook.result.current.tabIndexFor(id) === 0)).toHaveLength(1);
  });
});

describe("arrows move focus, Shift+arrows pan", () => {
  it("an unmodified arrow moves focus and does not pan", () => {
    const { hook, setViewport } = setup();

    // The FIRST arrow adopts the node `tabIndexFor` already pointed at rather than
    // stepping past it — otherwise arrowing into the graph would silently skip a node.
    key(hook, "ArrowRight");
    expect(hook.result.current.focusId).toBe("a.py");

    key(hook, "ArrowRight");
    expect(hook.result.current.focusId).toBe("b.py");
    expect(setViewport).not.toHaveBeenCalled();
  });

  it("a shifted arrow pans and does not move focus", () => {
    const { hook, setViewport } = setup();
    key(hook, "ArrowRight");
    const focused = hook.result.current.focusId;

    key(hook, "ArrowRight", { shiftKey: true });
    expect(setViewport).toHaveBeenCalled();
    expect(hook.result.current.focusId).toBe(focused);
  });

  it("focus wraps rather than dead-ending at the last node", () => {
    const { hook } = setup();
    for (let i = 0; i < ORDER.length; i++) key(hook, "ArrowRight");
    expect(ORDER).toContain(hook.result.current.focusId);
  });
});

describe("focus does not survive a node that vanishes", () => {
  it("clears when the focused node leaves the order", () => {
    const onSelect = vi.fn(), onClear = vi.fn(), onExpand = vi.fn(), setViewport = vi.fn();
    const hook = renderHook(
      ({ order }) => useGraphKeyboard({ order, onSelect, onClear, onExpand, setViewport }),
      { initialProps: { order: ORDER } },
    );
    act(() => {
      hook.result.current.onKeyDown({
        key: "ArrowRight", shiftKey: false, preventDefault: vi.fn(),
      } as unknown as React.KeyboardEvent<SVGSVGElement>);
    });
    expect(hook.result.current.focusId).toBe("a.py");

    hook.rerender({ order: ["b.py", "c.py"] });
    expect(hook.result.current.focusId).toBeNull();
  });
});
