import * as React from "react";

import type { Pos } from "./layout";

export interface GraphPins {
  /** Dragged positions, overlaying the computed layout. */
  pins: Record<string, Pos>;
  /** Spread onto a node's `<g>`. Stops propagation so the canvas does not pan underneath. */
  nodeHandlers: (id: string) => {
    onPointerDown: (e: React.PointerEvent<SVGGElement>) => void;
    onPointerMove: (e: React.PointerEvent<SVGGElement>) => void;
    onPointerUp: (e: React.PointerEvent<SVGGElement>) => void;
  };
  /**
   * True if the gesture that just ended actually moved.
   *
   * A drag ends with a click event on the same node, and without this the user would select
   * whatever they were only trying to reposition.
   */
  consumedDrag: () => boolean;
  isPinned: (id: string) => boolean;
  clearPins: () => void;
  pinCount: number;
}

/**
 * Drag-to-pin for graph nodes (PRD-20 D2).
 *
 * Dragging writes a position here and does NOT trigger a re-layout: the node follows the
 * pointer, everything else stays exactly where the user left it. The pin is then honoured by
 * the next layout run, so a data refresh or a manual re-layout settles the graph *around* the
 * nodes the user has decided matter.
 */
export function useGraphPins(toWorld: (clientX: number, clientY: number) => Pos): GraphPins {
  const [pins, setPins] = React.useState<Record<string, Pos>>({});
  const dragging = React.useRef<string | null>(null);
  const moved = React.useRef(false);

  const nodeHandlers = React.useCallback(
    (id: string) => ({
      onPointerDown: (e: React.PointerEvent<SVGGElement>) => {
        e.stopPropagation();
        dragging.current = id;
        moved.current = false;
        e.currentTarget.setPointerCapture?.(e.pointerId);
      },
      onPointerMove: (e: React.PointerEvent<SVGGElement>) => {
        if (dragging.current !== id) return;
        e.stopPropagation();
        moved.current = true;
        const p = toWorld(e.clientX, e.clientY);
        setPins((prev) => ({ ...prev, [id]: p }));
      },
      onPointerUp: (e: React.PointerEvent<SVGGElement>) => {
        if (dragging.current !== id) return;
        e.stopPropagation();
        dragging.current = null;
        e.currentTarget.releasePointerCapture?.(e.pointerId);
      },
    }),
    [toWorld],
  );

  const consumedDrag = React.useCallback(() => {
    const was = moved.current;
    moved.current = false;
    return was;
  }, []);

  const isPinned = React.useCallback((id: string) => pins[id] !== undefined, [pins]);
  const clearPins = React.useCallback(() => setPins({}), []);

  return {
    pins,
    nodeHandlers,
    consumedDrag,
    isPinned,
    clearPins,
    pinCount: Object.keys(pins).length,
  };
}
