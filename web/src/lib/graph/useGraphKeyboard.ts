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

/**
 * Canvas listbox (PRD-52 D6). Not the SVG hook above.
 *
 * The SVG graph moves real DOM focus with a roving tabindex. A canvas has no
 * node elements to focus, and a 2,500-node map cannot mount one row each.
 * This listbox is the one tab stop: `aria-activedescendant` names the active
 * node, and only `LISTBOX_WINDOW` options are in the DOM. Degree order is the
 * whole map — the viewport never decides who is in it.
 */
export const LISTBOX_WINDOW = 30;
const TYPEAHEAD_MS = 500;

const KIND_LABEL: Record<string, string> = {
  module: "Module",
  file: "File",
  symbol: "Symbol",
  doc: "Doc",
  config: "Config",
};

export function kindLabel(kind: string): string {
  if (KIND_LABEL[kind]) return KIND_LABEL[kind];
  if (!kind) return "Node";
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

/** `known: false` means presence was not read. `label: null` means it was, and named nobody. */
export interface HolderReading {
  known: boolean;
  label?: string | null;
}

export interface CanvasListNode {
  id: string;
  path: string;
  kind: string;
  name: string;
  degree: number;
}

export function degreeOrder<T extends { id: string; path: string; degree: number }>(nodes: T[]): T[] {
  return [...nodes].sort(
    (a, b) => b.degree - a.degree || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
}

/** No wrap. A listbox has Home and End for the ends; arrows stop there. `-1` is "nothing active yet". */
export function stepIndex(count: number, active: number, delta: number): number {
  if (count <= 0) return -1;
  if (active < 0 || active >= count) return delta < 0 ? count - 1 : 0;
  const next = active + delta;
  if (next < 0) return 0;
  if (next >= count) return count - 1;
  return next;
}

export function windowBounds(
  count: number,
  activeIndex: number,
  size = LISTBOX_WINDOW,
): { start: number; end: number } {
  if (count <= 0) return { start: 0, end: 0 };
  const n = Math.min(size, count);
  if (activeIndex < 0) return { start: 0, end: n };
  let start = activeIndex - Math.floor(n / 2);
  if (start < 0) start = 0;
  if (start + n > count) start = Math.max(0, count - n);
  return { start, end: start + n };
}

function typeaheadHit(node: { name: string; path: string }, query: string): boolean {
  const name = node.name.toLowerCase();
  const path = node.path.toLowerCase();
  const base = path.split("/").pop() ?? path;
  return name.startsWith(query) || path.startsWith(query) || base.startsWith(query);
}

/** Multi-character buffer refines the current hit. A new character searches after it, then wraps. */
export function matchTypeahead(
  nodes: { name: string; path: string }[],
  fromIndex: number,
  query: string,
): number {
  const q = query.trim().toLowerCase();
  if (!q || nodes.length === 0) return -1;
  const start = fromIndex >= 0 && fromIndex < nodes.length ? fromIndex : -1;
  if (q.length > 1 && start >= 0 && typeaheadHit(nodes[start], q)) return start;
  for (let step = 1; step <= nodes.length; step++) {
    const i = ((start < 0 ? -1 : start) + step) % nodes.length;
    if (typeaheadHit(nodes[i], q)) return i;
  }
  return -1;
}

export function holderClause(reading: HolderReading | undefined): string {
  if (!reading || !reading.known) return "holder unknown";
  if (!reading.label) return "not held";
  return `held by ${reading.label}`;
}

export function announceNode(
  node: { kind: string; path: string; degree: number },
  holder: HolderReading | undefined,
): string {
  return `${kindLabel(node.kind)} ${node.path}, ${node.degree} connections, ${holderClause(holder)}`;
}

export interface CanvasListboxRow {
  id: string;
  index: number;
  optionId: string;
  label: string;
}

export interface CanvasListbox {
  activeId: string | null;
  activeIndex: number;
  rows: CanvasListboxRow[];
  setSize: number;
  live: string;
  activeOptionId: string | null;
  onKeyDown: (e: React.KeyboardEvent) => void;
}

export function useCanvasListbox({
  nodes,
  selectedId,
  onSelect,
  onClearSelection,
  onFollow,
  holderOf,
  pathPending = false,
  pathArmed = false,
  onAbortPath,
  onDisarmPath,
  idPrefix,
  keepOrder = false,
  announce,
}: {
  nodes: CanvasListNode[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onClearSelection: () => void;
  /** Fired in the key handler, before paint. The camera tween must not be awaited. */
  onFollow: (id: string) => void;
  holderOf?: (id: string) => HolderReading;
  pathPending?: boolean;
  pathArmed?: boolean;
  onAbortPath?: () => void;
  onDisarmPath?: () => void;
  idPrefix: string;
  /** The caller's order is the traversal order (a flow of stages), not degree order. */
  keepOrder?: boolean;
  /** Replaces the code-graph sentence for views whose nodes are not files. */
  announce?: (id: string) => string;
}): CanvasListbox {
  const order = React.useMemo(() => (keepOrder ? nodes : degreeOrder(nodes)), [nodes, keepOrder]);
  const announceRef = React.useRef(announce);
  announceRef.current = announce;
  const say = (node: CanvasListNode) =>
    announceRef.current ? announceRef.current(node.id) : announceNode(node, holderRef.current?.(node.id));
  const [activeId, setActiveId] = React.useState<string | null>(null);
  const activeRef = React.useRef<string | null>(null);
  const orderRef = React.useRef(order);
  orderRef.current = order;
  const onSelectRef = React.useRef(onSelect);
  onSelectRef.current = onSelect;
  const onClearRef = React.useRef(onClearSelection);
  onClearRef.current = onClearSelection;
  const onFollowRef = React.useRef(onFollow);
  onFollowRef.current = onFollow;
  const holderRef = React.useRef(holderOf);
  holderRef.current = holderOf;
  const pathRef = React.useRef({ pending: pathPending, armed: pathArmed });
  pathRef.current = { pending: pathPending, armed: pathArmed };
  const onAbortRef = React.useRef(onAbortPath);
  onAbortRef.current = onAbortPath;
  const onDisarmRef = React.useRef(onDisarmPath);
  onDisarmRef.current = onDisarmPath;
  const selectedRef = React.useRef(selectedId);
  selectedRef.current = selectedId;
  const typeBuf = React.useRef("");
  const typeAt = React.useRef(0);

  React.useEffect(() => {
    if (activeRef.current && !order.some((n) => n.id === activeRef.current)) {
      activeRef.current = null;
      setActiveId(null);
    }
  }, [order]);

  const onKeyDown = React.useCallback((e: React.KeyboardEvent) => {
    const indexOf = (id: string | null) => {
      if (!id) return -1;
      return orderRef.current.findIndex((n) => n.id === id);
    };
    const activateIndex = (index: number) => {
      const node = orderRef.current[index];
      if (!node) return;
      // The ref moves in this handler. React state can batch; the next key must not.
      activeRef.current = node.id;
      setActiveId(node.id);
      onFollowRef.current(node.id);
    };
    const count = orderRef.current.length;
    const move = (delta: number) => {
      if (count === 0) return;
      e.preventDefault();
      e.stopPropagation();
      activateIndex(stepIndex(count, indexOf(activeRef.current), delta));
    };
    switch (e.key) {
      case "ArrowDown":
      case "ArrowRight":
        move(1);
        return;
      case "ArrowUp":
      case "ArrowLeft":
        move(-1);
        return;
      case "Home":
        if (count === 0) return;
        e.preventDefault();
        e.stopPropagation();
        activateIndex(0);
        return;
      case "End":
        if (count === 0) return;
        e.preventDefault();
        e.stopPropagation();
        activateIndex(count - 1);
        return;
      case "Enter": {
        const id = activeRef.current;
        if (!id) return;
        e.preventDefault();
        e.stopPropagation();
        onSelectRef.current(id);
        return;
      }
      case "Escape": {
        e.preventDefault();
        e.stopPropagation();
        // Pending, then armed, then the selection. One press undoes one layer.
        if (pathRef.current.pending && onAbortRef.current) {
          onAbortRef.current();
          return;
        }
        if (pathRef.current.armed && onDisarmRef.current) {
          onDisarmRef.current();
          return;
        }
        if (selectedRef.current) {
          onClearRef.current();
          return;
        }
        activeRef.current = null;
        setActiveId(null);
        return;
      }
      default:
        break;
    }
    if (e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === " ") return;
    const t = Date.now();
    const prev = t - typeAt.current <= TYPEAHEAD_MS ? typeBuf.current : "";
    const query = prev + e.key;
    typeBuf.current = query;
    typeAt.current = t;
    const idx = matchTypeahead(orderRef.current, indexOf(activeRef.current), query);
    if (idx >= 0) activateIndex(idx);
  }, []);

  const activeIndex = activeId ? order.findIndex((n) => n.id === activeId) : -1;
  const bounds = windowBounds(order.length, activeIndex);
  const rows: CanvasListboxRow[] = [];
  for (let i = bounds.start; i < bounds.end; i++) {
    const node = order[i];
    rows.push({
      id: node.id,
      index: i,
      optionId: `${idPrefix}-opt-${i}`,
      label: say(node),
    });
  }
  const live = activeIndex >= 0 ? say(order[activeIndex]) : "";

  return {
    activeId,
    activeIndex,
    rows,
    setSize: order.length,
    live,
    activeOptionId: activeIndex >= 0 ? `${idPrefix}-opt-${activeIndex}` : null,
    onKeyDown,
  };
}
