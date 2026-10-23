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

/** Screen pixels a pointer must travel before a canvas node drag becomes a pin. */
export const DRAG_PIN_PX = 4;

export interface CanvasPin {
  x: number;
  y: number;
  areaKey: string;
}

/** Session pins keyed by layout id, then node id. Not written anywhere durable. */
export type PinsByLayout = Record<string, Record<string, CanvasPin>>;

const EMPTY_ROW: Record<string, CanvasPin> = {};

export function pinsFor(all: PinsByLayout, layoutId: string): Record<string, CanvasPin> {
  return all[layoutId] ?? EMPTY_ROW;
}

export function pinTotal(all: PinsByLayout): number {
  let n = 0;
  for (const row of Object.values(all)) n += Object.keys(row).length;
  return n;
}

/** One layout's pin. Other layouts keep theirs. */
export function withPin(
  all: PinsByLayout,
  layoutId: string,
  id: string,
  pin: CanvasPin,
): PinsByLayout {
  const row = all[layoutId];
  const prev = row?.[id];
  if (prev && prev.x === pin.x && prev.y === pin.y && prev.areaKey === pin.areaKey) return all;
  return { ...all, [layoutId]: { ...row, [id]: pin } };
}

/**
 * Drop pins whose area key no longer matches.
 *
 * A node that has left the map is dropped too, and is not counted: the toast is
 * for a regroup, which is an area-key change. `cleared` is that count.
 */
export function dropRegrouped(
  all: PinsByLayout,
  areaKeyOf: (id: string) => string | undefined,
): { pins: PinsByLayout; cleared: number } {
  let cleared = 0;
  let changed = false;
  const next: PinsByLayout = {};
  for (const [layoutId, row] of Object.entries(all)) {
    let rowChanged = false;
    const kept: Record<string, CanvasPin> = {};
    for (const [id, pin] of Object.entries(row)) {
      const area = areaKeyOf(id);
      if (area === undefined || area !== pin.areaKey) {
        rowChanged = true;
        if (area !== undefined) cleared += 1;
        continue;
      }
      kept[id] = pin;
    }
    if (!rowChanged) {
      next[layoutId] = row;
      continue;
    }
    changed = true;
    if (Object.keys(kept).length > 0) next[layoutId] = kept;
  }
  if (!changed) return { pins: all, cleared: 0 };
  return { pins: next, cleared };
}

export interface CanvasPinsApi {
  byLayout: PinsByLayout;
  place: (layoutId: string, id: string, x: number, y: number, areaKey: string) => void;
  clearAll: () => void;
  /** Returns how many pins a regroup dropped. Updates the store when any did. */
  reconcile: (areaKeyOf: (id: string) => string | undefined) => number;
  /** Latest pins, including a reconcile that has not re-rendered yet. */
  read: () => PinsByLayout;
  total: number;
}

/**
 * Canvas pins (PRD-52 D6). Separate from {@link useGraphPins}: that hook is the
 * SVG graph, one layout, writing on every move. Canvas pins are keyed by layout
 * id, remember the node's area key, and the view commits them on drop.
 *
 * The view holds this store so a trip through Matrix, which unmounts the canvas,
 * does not throw the session away.
 */
export function useCanvasPins(): CanvasPinsApi {
  const [byLayout, setByLayout] = React.useState<PinsByLayout>({});
  const ref = React.useRef(byLayout);
  ref.current = byLayout;

  const read = React.useCallback(() => ref.current, []);

  const place = React.useCallback(
    (layoutId: string, id: string, x: number, y: number, areaKey: string) => {
      setByLayout((prev) => {
        const next = withPin(prev, layoutId, id, { x, y, areaKey });
        ref.current = next;
        return next;
      });
    },
    [],
  );

  const clearAll = React.useCallback(() => {
    ref.current = {};
    setByLayout({});
  }, []);

  const reconcile = React.useCallback((areaKeyOf: (id: string) => string | undefined) => {
    const result = dropRegrouped(ref.current, areaKeyOf);
    if (result.pins !== ref.current) {
      ref.current = result.pins;
      setByLayout(result.pins);
    }
    return result.cleared;
  }, []);

  const total = pinTotal(byLayout);

  return React.useMemo(
    () => ({ byLayout, place, clearAll, reconcile, read, total }),
    [byLayout, place, clearAll, reconcile, read, total],
  );
}
