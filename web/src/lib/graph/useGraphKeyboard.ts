import * as React from "react";

import type { Viewport } from "./useGraphViewport";

/** How far one arrow press slides the view, in viewBox units. */
const PAN_STEP = 60;

/**
 * Nodes are NEVER tab stops. The `<svg>` is the single stop; nodes are reached with the
 * arrow keys and focused programmatically, which `tabIndex={-1}` permits and `0` would not
 * require.
 *
 * This was a roving `0`, which read as the standard pattern and was not, because the
 * container is `tabIndex={0}` too: Tab into the graph landed on the `<svg>`, and Tab again
 * landed on a node INSIDE it. Two stops where the docstring below promises one. The hook's
 * own test could not see it — a `renderHook` test has no container to check, so "exactly one
 * node is tabbable" was true and irrelevant at the same time (GRPH-479).
 */
export const NODE_TAB_INDEX = -1;

export interface GraphKeyboard {
  /** The node that currently owns focus, or null when focus is on the canvas itself. */
  focusId: string | null;
  setFocusId: (id: string | null) => void;
  /**
   * Ref callback for a node's `<g>`. Registering is what lets arrow keys move REAL focus
   * rather than only the ring that draws it.
   */
  registerNode: (id: string) => (el: SVGGElement | null) => void;
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

  const nodes = React.useRef(new Map<string, SVGGElement>());
  const refs = React.useRef(new Map<string, (el: SVGGElement | null) => void>());

  // One stable callback per id. A fresh function each render would make React detach and
  // reattach every node on every layout tick.
  const registerNode = React.useCallback((id: string) => {
    let cb = refs.current.get(id);
    if (!cb) {
      cb = (el: SVGGElement | null) => {
        if (el) nodes.current.set(id, el);
        else nodes.current.delete(id);
      };
      refs.current.set(id, cb);
    }
    return cb;
  }, []);

  /**
   * Move REAL focus to the focused node.
   *
   * Without this the arrow keys moved `focusId` — which draws the ring — and nothing else.
   * DOM focus stayed on whatever was focused first, so a screen reader announced nothing as
   * the user arrowed across the graph, and every `aria-label` this view builds so carefully
   * went unread. The bug was invisible to the suite because the tests asserted on `focusId`,
   * the state the code sets, rather than on `document.activeElement`, the thing assistive
   * technology reads (GRPH-479).
   *
   * Guarded on focus already being inside this graph: `focusId` also moves when something
   * else selects a node, and a panel elsewhere on the page must not have focus yanked out
   * from under it.
   */
  React.useEffect(() => {
    if (focusId === null) return;
    const el = nodes.current.get(focusId);
    if (!el) return;
    const active = document.activeElement;
    const svg = el.ownerSVGElement;
    if (!svg || !(active === svg || svg.contains(active))) return;
    if (active === el) return;
    // `preventScroll`: focusing inside a transformed SVG otherwise makes the scroll
    // container jump, which reads as the graph lurching on every arrow press.
    el.focus({ preventScroll: true });
  }, [focusId]);

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

  return { focusId, setFocusId, registerNode, onKeyDown };
}
