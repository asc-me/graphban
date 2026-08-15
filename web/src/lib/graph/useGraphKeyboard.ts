import * as React from "react";

import type { Viewport } from "./useGraphViewport";

/** How far one arrow press slides the view, in viewBox units. */
const PAN_STEP = 60;

export interface GraphKeyboard {
  /** The node that currently owns focus, or null when focus is on the canvas itself. */
  focusId: string | null;
  setFocusId: (id: string | null) => void;
  /** Roving tabindex: exactly one node is tabbable at a time. */
  tabIndexFor: (id: string) => 0 | -1;
  onKeyDown: (e: React.KeyboardEvent<SVGSVGElement>) => void;
}

export interface GraphKeyboardOpts {
  /** Traversal order. Callers pass degree order so Tab/arrows meet the hubs first. */
  order: string[];
  onSelect: (id: string) => void;
  onClear: () => void;
  /** Widen the highlight by a ring — the keyboard equivalent of shift-click. */
  onExpand: () => void;
  setViewport: React.Dispatch<React.SetStateAction<Viewport>>;
}

/**
 * Keyboard navigation for the graph (PRD-20 D3, G6).
 *
 * **Roving tabindex, not a tab stop per node.** The PRD said `Tab` walks the nodes in degree
 * order, which on a 500-node graph is 500 tab stops between the toolbar and whatever follows
 * the graph — a keyboard user has to press Tab five hundred times to get past a decoration.
 * The standard composite-widget pattern is used instead: the `<svg>` is ONE tab stop, and once
 * inside, arrow keys move focus node to node. Every node stays reachable and the graph costs a
 * keyboard user exactly one Tab to skip.
 *
 * **Arrows move focus; Shift+arrows pan.** The PRD gave arrows to panning, which cannot
 * coexist with arrow-key focus movement. Focus wins the unmodified keys because reaching a
 * node is the accessibility requirement and panning is a convenience — and panning is still
 * available by drag, by the reset control, and by Shift+arrow.
 *
 * Both divergences are recorded on GRPH-386 rather than applied silently.
 */
export function useGraphKeyboard({
  order,
  onSelect,
  onClear,
  onExpand,
  setViewport,
}: GraphKeyboardOpts): GraphKeyboard {
  const [focusId, setFocusId] = React.useState<string | null>(null);

  // A node that vanishes (filter, refetch) must not keep phantom focus.
  React.useEffect(() => {
    if (focusId && !order.includes(focusId)) setFocusId(null);
  }, [order, focusId]);

  const step = React.useCallback(
    (delta: number) => {
      if (order.length === 0) return;
      setFocusId((cur) => {
        if (cur === null) return order[delta > 0 ? 0 : order.length - 1];
        const i = order.indexOf(cur);
        if (i === -1) return order[0];
        // Wrap: a graph is a loop to walk, not a list with a dead end at each side.
        return order[(i + delta + order.length) % order.length];
      });
    },
    [order],
  );

  const onKeyDown = React.useCallback(
    (e: React.KeyboardEvent<SVGSVGElement>) => {
      const pan = (dx: number, dy: number) =>
        setViewport((v) => ({ ...v, x: v.x + dx, y: v.y + dy }));

      switch (e.key) {
        case "ArrowRight":
        case "ArrowDown":
          e.preventDefault();
          if (e.shiftKey) pan(-PAN_STEP, e.key === "ArrowDown" ? -PAN_STEP : 0);
          else step(1);
          break;
        case "ArrowLeft":
        case "ArrowUp":
          e.preventDefault();
          if (e.shiftKey) pan(PAN_STEP, e.key === "ArrowUp" ? PAN_STEP : 0);
          else step(-1);
          break;
        case "Home":
          e.preventDefault();
          setFocusId(order[0] ?? null);
          break;
        case "End":
          e.preventDefault();
          setFocusId(order[order.length - 1] ?? null);
          break;
        case "Enter":
        case " ":
          if (!focusId) return;
          e.preventDefault();
          // Shift+Enter mirrors shift-click: select and widen by a ring in one gesture.
          onSelect(focusId);
          if (e.shiftKey) onExpand();
          break;
        case "Escape":
          e.preventDefault();
          onClear();
          setFocusId(null);
          break;
        default:
          break;
      }
    },
    [focusId, onClear, onExpand, onSelect, order, setViewport, step],
  );

  const tabIndexFor = React.useCallback(
    (id: string): 0 | -1 => {
      // Exactly one tabbable node: the focused one, or the first when nothing has focus yet.
      if (focusId === null) return order[0] === id ? 0 : -1;
      return focusId === id ? 0 : -1;
    },
    [focusId, order],
  );

  return { focusId, setFocusId, tabIndexFor, onKeyDown };
}
