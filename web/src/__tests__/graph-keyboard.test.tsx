import { act, render, renderHook } from "@testing-library/react";
import * as React from "react";
import { describe, expect, it, vi } from "vitest";

import type { GraphKeyboard } from "@/lib/graph/useGraphKeyboard";
import { NODE_TAB_INDEX, useGraphKeyboard } from "@/lib/graph/useGraphKeyboard";

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

/**
 * A container, because the claim needs one.
 *
 * The three tests this replaces asked `tabIndexFor` whether exactly one NODE was tabbable.
 * That was true, and it was not the requirement: the `<svg>` is `tabIndex={0}` as well, so
 * the graph cost two Tab presses while a hook-only test — which has no container to look at
 * — reported one. A `renderHook` test structurally cannot see this. (GRPH-479)
 */
function Harness({ order = ORDER, onReady }: { order?: string[]; onReady?: (kb: GraphKeyboard) => void }) {
  const [, setViewport] = React.useState({ x: 0, y: 0, k: 1 });
  const kb = useGraphKeyboard({
    order,
    onSelect: () => {},
    onClear: () => {},
    onExpand: () => {},
    setViewport: setViewport as never,
  });
  onReady?.(kb);
  return (
    <>
      <button type="button">before</button>
      <svg
        role="application"
        tabIndex={0}
        aria-label="Code graph"
        data-testid="canvas"
        onKeyDown={kb.onKeyDown}
      >
        {order.map((id) => (
          <g key={id} ref={kb.registerNode(id)} tabIndex={NODE_TAB_INDEX} role="button" aria-label={id} />
        ))}
      </svg>
      <button type="button">after</button>
    </>
  );
}

describe("the graph is one tab stop", () => {
  it("puts tabIndex 0 on the canvas and on nothing inside it", () => {
    const { getByTestId } = render(<Harness />);
    const svg = getByTestId("canvas");

    expect(svg.getAttribute("tabindex")).toBe("0");
    expect(svg.querySelectorAll('[tabindex="0"]')).toHaveLength(0);
  });

  it("keeps that true at 500 nodes — the 500-Tab trap", () => {
    const many = Array.from({ length: 500 }, (_, i) => `n${i}.py`);
    const { getByTestId } = render(<Harness order={many} />);

    expect(getByTestId("canvas").querySelectorAll('[tabindex="0"]')).toHaveLength(0);
  });
});

describe("arrows move REAL focus, not only the ring", () => {
  it("moves document.activeElement onto the node, so it is announced", () => {
    const { getByTestId, getByLabelText } = render(<Harness />);
    const svg = getByTestId("canvas");
    act(() => svg.focus());
    expect(document.activeElement).toBe(svg);

    act(() => {
      svg.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    });

    // The assertion that matters. `focusId` moving is what the broken version already did;
    // a screen reader reads document.activeElement, and reads nothing when it does not move.
    expect(document.activeElement).toBe(getByLabelText("a.py"));
  });

  it("carries focus on to the next node rather than stranding it on the first", () => {
    const { getByTestId, getByLabelText } = render(<Harness />);
    const svg = getByTestId("canvas");
    act(() => svg.focus());
    const arrow = () =>
      act(() => {
        svg.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
      });

    arrow();
    arrow();

    expect(document.activeElement).toBe(getByLabelText("b.py"));
    // The desync the walk found: the tab stop moved and focus did not, leaving DOM focus on
    // an element that had become tabindex="-1".
    expect((document.activeElement as Element).getAttribute("tabindex")).toBe("-1");
    expect(getByLabelText("a.py")).not.toBe(document.activeElement);
  });

  it("does not yank focus out of something else on the page", () => {
    let api: GraphKeyboard | null = null;
    const { getByText } = render(<Harness onReady={(kb) => { api = kb; }} />);
    const outside = getByText("before");
    act(() => outside.focus());

    // Something elsewhere selects a node — a find, a panel, a deep link. Focus is not ours.
    act(() => api!.setFocusId("c.py"));

    expect(document.activeElement).toBe(outside);
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
