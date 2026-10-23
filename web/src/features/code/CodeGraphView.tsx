import * as React from "react";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { DETAIL_BUDGET } from "@/lib/graph/galaxy";
import {
  areaColor,
  areaOf,
  areasAtScale,
  areasForChips,
  groupAreas,
  parentMap,
} from "@/lib/graph/areas";
import { degrees, topByDegree, withinHops } from "@/lib/graph/metrics";
import {
  cloudsFor,
  contentionOf,
  describeContention,
  formatRemaining,
  indexByNode,
  nodesHeldBy,
  roleHex,
  secondsRemaining,
} from "@/lib/graph/presence";
import { useGraphFind, type GraphFind } from "@/lib/graph/useGraphFind";
import { pathHighlight } from "@/lib/graph/pathHighlight";
import { NODE_TAB_INDEX, useGraphKeyboard } from "@/lib/graph/useGraphKeyboard";
import { HubsPanel, type HubSort } from "./HubsPanel";
import { PathTrace } from "./PathTrace";
import { useGraphLayout } from "@/lib/graph/useGraphLayout";
import { useCanvasPins, useGraphPins } from "@/lib/graph/useGraphPins";
import {
  LABEL_ZOOM,
  MAX_ZOOM,
  MIN_ZOOM,
  useGraphViewport,
  zoomAbout,
} from "@/lib/graph/useGraphViewport";
import { FETCH_FAILED, PlannerError } from "@/components/planner/PlannerStates";
import { useCodeAnalysis, useCodeMap, useFleetPresence } from "@/lib/queries";
import type { CodeEdgeType, CodeNeighbors, HeldArea, ProjectStub } from "@/lib/types";

import { AreaMatrix, CellInspector, buildMatrix, cellFromModel, type GraphLayoutId } from "./AreaMatrix";
import {
  LINKED_NODE_TOAST,
  readGraphUrl,
  replaceGraphUrl,
  type GraphUrlSeed,
} from "./useGraphUrlState";
import { CANVAS_MAX_K, CANVAS_MIN_K } from "./canvas/camera";
import { useCanvasPath } from "./canvas/overlay";
import { CodeChat } from "./CodeChat";
import { CodeGraphCanvas, type CanvasControlApi } from "./CodeGraphCanvas";
import { FleetLegend } from "./FleetLegend";

const KIND_META: Record<string, { label: string; color: string }> = {
  module: { label: "Module", color: "#c6f24e" },
  file: { label: "File", color: "#7ca2ff" },
  symbol: { label: "Symbol", color: "#a78bfa" },
  // GRPH-381. Muted against the three code kinds on purpose: docs and config are part of the
  // blast radius of a change, not part of the call graph, and colouring them as loudly as a
  // module would say they are the same kind of thing. Both values are already in the token set
  // (`--color-st-review`, `--color-muted-2`), so this adds no new colour vocabulary.
  doc: { label: "Doc", color: "#e0b34a" },
  config: { label: "Config", color: "#9aa3ab" },
};
const kindMeta = (kind: string) => KIND_META[kind] ?? { label: kind || "node", color: "#8b949e" };

/**
 * Build.dc.html encodes kind as SHAPE and area as COLOUR. Shape landed in GRPH-1033; fill
 * now follows `areaOf` (the owning module, else the path prefix) hashed onto the existing
 * token set. Presence stays on clouds/rings (PRD-20 D5) — a held node must still say what
 * kind it is, and that is now the glyph, not the fill.
 *
 * The legend is grey shapes: once fill names area, a coloured key would re-teach kind as
 * colour, which is the channel we just freed.
 */
const KIND_LEGEND_COLOR = "#9aa3ab"; // --color-muted-2
type KindShape = "ring" | "circle" | "diamond" | "square";
function kindShape(kind: string): KindShape {
  if (kind === "module") return "ring";
  if (kind === "symbol" || kind === "request") return "diamond";
  if (kind === "doc" || kind === "config") return "square";
  return "circle";
}

/** Radii from Build.dc.html, scaled to this viewBox. File size still grows with degree. */
function kindRadius(kind: string, degree = 0): number {
  if (kind === "module") return 11;
  if (kind === "file") return 5.5 + Math.min(degree, 6) * 0.45;
  if (kind === "symbol") return 5;
  if (kind === "doc" || kind === "config") return 5.5;
  return 7;
}

function KindGlyph({
  kind,
  color,
  r,
  described = true,
  stale = false,
}: {
  kind: string;
  color: string;
  r: number;
  described?: boolean;
  stale?: boolean;
}) {
  const shape = kindShape(kind);
  const dash = !described || stale ? "2 2" : undefined;
  const fill = described ? (shape === "ring" ? "#0a0c0e" : color) : "#0d1114";
  const stroke = described && shape !== "ring" ? "#0a0c0e" : color;
  const opacity = described ? 1 : 0.7;
  if (shape === "diamond") {
    return (
      <polygon
        points={`0,${-r} ${r},0 0,${r} ${-r},0`}
        fill={fill}
        stroke={stroke}
        strokeWidth={2}
        strokeDasharray={dash}
        opacity={opacity}
      />
    );
  }
  if (shape === "square") {
    return (
      <rect
        x={-r}
        y={-r}
        width={r * 2}
        height={r * 2}
        rx={1.5}
        fill={fill}
        stroke={stroke}
        strokeWidth={2}
        strokeDasharray={dash}
        opacity={opacity}
      />
    );
  }
  return (
    <>
      <circle
        r={r}
        fill={fill}
        stroke={stroke}
        strokeWidth={2}
        strokeDasharray={dash}
        opacity={opacity}
      />
      {shape === "ring" && described && (
        <circle r={Math.max(2.2, r * 0.36)} fill={color} />
      )}
    </>
  );
}

const EDGE_META: Record<CodeEdgeType, { label: string; color: string }> = {
  imports: { label: "imports", color: "#7ca2ff" },
  calls: { label: "calls", color: "#c6f24e" },
  owns: { label: "owns", color: "#a78bfa" },
  tested_by: { label: "tested by", color: "#5fd07a" },
  references: { label: "references", color: "#e0b34a" },
};
const EDGE_TYPES = Object.keys(EDGE_META) as CodeEdgeType[];

const W = 900;
const H = 560;
/** How many hubs keep their name when zoomed out (PRD-20 D2, level of detail). */
const LOD_TOP_N = 12;

/** Short label for a node: its name, else the last path segment (after `/` or `::`). */
function label(path: string, name: string): string {
  if (name) return name;
  const seg = path.split("::").pop() ?? path;
  const parts = seg.split("/").filter(Boolean);
  return parts.pop() ?? seg;
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function HoverTip({
  x,
  y,
  title,
  sub,
}: {
  x: number;
  y: number;
  title: string;
  sub: string;
}) {
  const w = Math.min(220, Math.max(96, title.length * 7 + 20));
  const h = sub ? 36 : 22;
  return (
    <g
      transform={`translate(${x},${y})`}
      pointerEvents="none"
      role="tooltip"
      aria-label={sub ? `${title}: ${sub}` : title}
    >
      <rect
        x={-w / 2}
        y={-h}
        width={w}
        height={h}
        rx={7}
        fill="#12171b"
        stroke="#2a333a"
      />
      <text
        y={sub ? -h + 14 : -h / 2 + 4}
        textAnchor="middle"
        fontSize={11.5}
        fontFamily="IBM Plex Mono, monospace"
        fill="#e6e9ec"
      >
        {title}
      </text>
      {sub ? (
        <text
          y={-8}
          textAnchor="middle"
          fontSize={10.5}
          fontFamily="IBM Plex Mono, monospace"
          fill="#8b949e"
        >
          {sub}
        </text>
      ) : null}
    </g>
  );
}

export function CodeGraphView() {
  // D10: one read, on mount. Later search-string edits do not move the view.
  const urlSeed = React.useRef<GraphUrlSeed | null>(null);
  if (urlSeed.current === null) urlSeed.current = readGraphUrl(window.location.search);
  const seed = urlSeed.current;
  const { activeId } = useProjectCtx();
  const mapQ = useCodeMap(activeId);
  const { data: map, isLoading } = mapQ;
  const [enabled, setEnabled] = React.useState<Record<CodeEdgeType, boolean>>({
    imports: true, calls: true, owns: true, tested_by: true, references: true,
  });
  const [selPath, setSelPath] = React.useState<string | null>(seed.sel);
  /** Find and hub picks tween the canvas camera. A canvas click does not. */
  const [frameId, setFrameId] = React.useState<string | null>(null);
  const [hubsOpen, setHubsOpen] = React.useState(false);
  // AC-19. `armed` is the discoverable way in: alt-click is the PRD's gesture and is invisible,
  // and on some window managers alt-drag never reaches the page at all.
  const [pathTo, setPathTo] = React.useState<string | null>(null);
  const [armed, setArmed] = React.useState(false);
  const [hubSort, setHubSort] = React.useState<HubSort>("inbound");
  const [nb, setNb] = React.useState<CodeNeighbors | null>(null);
  const [hoverId, setHoverId] = React.useState<string | null>(null);
  // How many rings the selection lights. Shift-click (or Shift+Enter) widens it; a fresh
  // selection resets it, so depth never quietly persists into the next thing you click.
  const [depth, setDepth] = React.useState(seed.depth ?? 1);
  // Isolate (Build.dc.html): the design's toggle beside the depth chips. The canvas already
  // DIMS everything outside the reach ring to 0.22; isolate drops those nodes and edges
  // instead, so a dense graph can be read at all. It needs a reach set to isolate against,
  // so with nothing selected the control is disabled rather than silently doing nothing.
  const [isolate, setIsolate] = React.useState(seed.iso === true);
  // Matrix is the fourth layout. It is DOM, so the canvas unmounts while it
  // is showing. Show in graph comes back to Clusters with this pair isolated.
  const [layoutMode, setLayoutMode] = React.useState<GraphLayoutId>(seed.layout ?? "clusters");
  // D2 holds the area depth for the session. The canvas owns the layout hook,
  // and opening Matrix unmounts it, so the depth lives here and goes back in.
  const [keptDepth, setKeptDepth] = React.useState<number | undefined>(undefined);
  const onAreaDepth = React.useCallback((depth: number) => {
    setKeptDepth((cur) => (cur === depth ? cur : depth));
  }, []);
  const [matrixCell, setMatrixCell] = React.useState<{ row: string; col: string } | null>(seed.cell);
  const [linkToast, setLinkToast] = React.useState<string | null>(null);
  // False until the mount-time URL has been checked against the loaded map.
  // Writing earlier would replace a shared link with the defaults.
  const [urlReady, setUrlReady] = React.useState(false);
  const [matrixScroll, setMatrixScroll] = React.useState(0);
  const matrixScrollRef = React.useRef<HTMLDivElement | null>(null);
  const [returnPoint, setReturnPoint] = React.useState<{
    row: string;
    col: string;
    scroll: number;
  } | null>(null);
  const [pairFocus, setPairFocus] = React.useState<{ src: string; dst: string } | null>(null);
  const [frameArea, setFrameArea] = React.useState<{ id: string; n: number } | null>(null);
  // Canvas mode draws with its own camera (0.7×–16×). The shared control
  // cluster reads this scale and drives that camera; the SVG viewport is a
  // different range and must not move the canvas.
  const canvasControlRef = React.useRef<CanvasControlApi | null>(null);
  const [canvasK, setCanvasK] = React.useState(1);
  const onCanvasZoom = React.useCallback((k: number) => setCanvasK(k), []);
  // Trail breadcrumb: the last N nodes the user selected, most-recent last. Clicking one
  // re-selects it without losing the rest — a back button that does not erase history.
  const [trail, setTrail] = React.useState<string[]>([]);
  // Which pane the 300px aside shows. The design has one aside and no tabs; the app has a
  // chat the design's own copy points at, so the tab strip is the seam between them.
  const [sidePane, setSidePane] = React.useState<"inspect" | "ask">("inspect");

  const nodes = map?.nodes ?? [];
  // The UNFILTERED set drives layout; the filtered one only decides what is drawn. That split
  // is what makes a chip toggle redraw instead of rearranging the map under the user (AC-3).
  const allEdges = React.useMemo(() => map?.edges ?? [], [map]);
  const isFiltered = EDGE_TYPES.some((t) => !enabled[t]);

  // Node ids are paths; include edge endpoints even if a node wasn't described (dangling).
  const allIds = React.useMemo(() => {
    const s = new Set<string>();
    nodes.forEach((nd) => s.add(nd.path));
    allEdges.forEach((e) => {
      s.add(e.src);
      s.add(e.dst);
    });
    return [...s].sort();
  }, [nodes, allEdges]);

  /**
   * ONE definition of "how big is this graph", because there were two and they disagreed
   * (GRPH-479). The caption read `map.node_count` — the server's count of DESCRIBED nodes —
   * while the canvas drew `allIds`, which is that set plus every edge endpoint nobody has
   * described yet. On the live graph those were 193 and 228, so a sighted reader and a
   * screen-reader user were told different things about the same picture, and the accessible
   * name was the accurate one.
   *
   * `drawn` is the honest headline: it is what is on screen, what is focusable, and what the
   * arrow keys move through. `undescribed` is surfaced rather than folded in, because "35 of
   * these are only names an edge pointed at" is the interesting half — it is the outstanding
   * `describe_code` work, and burying it in a total is how it stays invisible.
   */
  const counts = React.useMemo(() => ({
    drawn: allIds.length,
    described: nodes.length,
    undescribed: Math.max(0, allIds.length - nodes.length),
    edges: allEdges.length,
  }), [allIds, nodes, allEdges]);

  // Above the detail budget the code view is canvas mode (PRD-52 D1). The galaxy
  // collapse (PRD-20 D9) is gone (PRD-52 D8): every node is drawn, none is entered.
  const canvasMode = allIds.length > DETAIL_BUDGET;
  const ids = allIds;

  // The UNFILTERED set drives layout; the filtered one only decides what is drawn. That split
  // is what makes a chip toggle redraw instead of rearranging the map under the user (AC-3).
  const edges = React.useMemo(
    () => allEdges.filter((e) => enabled[e.type]),
    [allEdges, enabled],
  );

  const nodeByPath = React.useMemo(() => {
    const m: Record<string, (typeof nodes)[number]> = {};
    nodes.forEach((nd) => (m[nd.path] = nd));
    return m;
  }, [nodes]);

  // Canvas mode lays its own positions out in the worker. SVG mode lays out every node.
  const layoutIds = React.useMemo(() => (canvasMode ? [] : ids), [canvasMode, ids]);
  const layoutEdges = React.useMemo(
    () => allEdges.map((e) => ({ a: e.src, b: e.dst })),
    [allEdges],
  );

  const view = useGraphViewport(W, H);
  const pinsApi = useGraphPins(view.toWorld);
  // Held here, not in the canvas: Matrix unmounts the canvas, and a pin is
  // session state keyed by layout. The SVG pins above are a different store.
  const canvasPins = useCanvasPins();
  const kindOf = React.useMemo(() => {
    const m: Record<string, string> = {};
    for (const id of allIds) m[id] = nodeByPath[id]?.kind ?? "";
    return m;
  }, [allIds, nodeByPath]);
  const ownsParents = React.useMemo(
    () =>
      parentMap(
        allEdges.filter((e) => e.type === "owns").map((e) => ({ src: e.src, dst: e.dst })),
      ),
    [allEdges],
  );
  const membership = React.useMemo(() => {
    const m: Record<string, string> = {};
    for (const id of allIds) m[id] = areaOf(id, kindOf[id] ?? "", ownsParents, kindOf);
    return m;
  }, [allIds, kindOf, ownsParents]);

  const { pos: laidOut, pending, relayout } = useGraphLayout(layoutIds, layoutEdges, {
    width: W,
    height: H,
    pinned: pinsApi.pins,
    membership,
    kinds: kindOf,
  });
  // A dragged node sits where the user put it; everything else where the layout put it.
  const pos = React.useMemo(
    () => ({ ...laidOut, ...pinsApi.pins }),
    [laidOut, pinsApi.pins],
  );

  // Search the path AND the described name: a user hunting "claim_next" should find
  // `items.py::claim_next` whether they type the symbol or the file.
  const labelOf = React.useCallback(
    (id: string) => `${id} ${nodeByPath[id]?.name ?? ""}`,
    [nodeByPath],
  );
  // Find searches every node. There is no collapsed component to enter (PRD-52 D8):
  // a canvas hit selects the node and frames it.
  const find = useGraphFind(allIds, labelOf);

  const firstMatch = find.active ? [...find.matches].sort()[0] : undefined;
  React.useEffect(() => {
    if (!firstMatch || !canvasMode) return;
    setSelPath(firstMatch);
    setFrameId(firstMatch);
  }, [firstMatch, canvasMode]);
  const hubs = React.useMemo(() => topByDegree(ids, layoutEdges, LOD_TOP_N), [ids, layoutEdges]);

  // The kinds the legend has to explain: what is DRAWN, in the fixed order of the colour
  // vocabulary rather than whatever order the nodes arrived in, so the key does not reshuffle
  // itself when the map changes underneath it.
  const drawnKinds = React.useMemo(() => {
    const present = new Set(ids.map((id) => nodeByPath[id]?.kind).filter(Boolean) as string[]);
    return Object.keys(KIND_META).filter((k) => present.has(k));
  }, [ids, nodeByPath]);

  // Area chips under find (Build.dc.html `gAreas`). Colour and membership come from
  // `areaOf` — the owning module, else the path prefix — not from galaxy components
  // (those are a scale valve, and this project is ~95% one component).
  const areas = React.useMemo(() => {
    const groups = areasForChips(groupAreas(ids, membership), kindOf);
    return groups.map((g) => ({
      id: g.id,
      label: label(g.id, nodeByPath[g.id]?.name ?? ""),
      color: areaColor(g.id),
      meta: `${g.members.length} nodes`,
    }));
  }, [ids, membership, kindOf, nodeByPath]);
  const hullGroups = React.useMemo(
    () => groupAreas(ids, membership).filter((g) => g.members.length >= 2),
    [ids, membership],
  );

  // Fetch the rich neighborhood for the selected node (edges + touching items).
  React.useEffect(() => {
    if (!selPath) {
      setNb(null);
      return;
    }
    let cancelled = false;
    setNb(null);
    api
      .codeNeighbors(selPath, activeId)
      .then((res) => !cancelled && setNb(res))
      .catch(() => !cancelled && setNb(null));
    return () => {
      cancelled = true;
    };
  }, [selPath, activeId]);

  // Highlight set: everything within `depth` rings of the selection. A BFS rather than the
  // one-hop scan this replaced, so "expand by one ring" keeps working at ring two and three.
  const drawnEdges = React.useMemo(
    () => edges.map((e) => ({ a: e.src, b: e.dst })),
    [edges],
  );
  const hl = React.useMemo(() => {
    if (!selPath) return null;
    const hlNodes = withinHops(ids, drawnEdges, selPath, depth);
    const hlEdges = new Set<string>();
    edges.forEach((e, i) => {
      // An edge lights only when BOTH ends are in the reach set — otherwise the outermost
      // ring would trail half-edges into nodes that are themselves dimmed.
      if (hlNodes.has(e.src) && hlNodes.has(e.dst)) hlEdges.add(String(i));
    });
    return { hlNodes, hlEdges };
  }, [selPath, depth, ids, drawnEdges, edges]);

  // Hover previews the 1-hop neighbourhood without committing a selection — read-before-click,
  // which is what makes a dense graph explorable.
  const hovered = React.useMemo(
    () => (hoverId ? withinHops(ids, drawnEdges, hoverId, 1) : null),
    [hoverId, ids, drawnEdges],
  );

  // Presence (D4/D5). Polled at the cadence the server reports; the graph renders exactly what
  // the payload placed and never infers a holder for a node it did not name.
  const { data: presence } = useFleetPresence(activeId);
  // D4: dependencies that leave this repo. Empty outside an org — no siblings to depend
  // on, which is a different fact from having none.
  const outbound = map?.outbound ?? [];
  const [stubHover, setStubHover] = React.useState<ProjectStub | null>(null);
  const heldByNode = React.useMemo(() => indexByNode(presence), [presence]);
  // `presence` missing is "nobody looked". An empty held list is "looked, and this node is free".
  const holderOf = React.useCallback(
    (id: string) => {
      if (!presence) return { known: false as const };
      const holders = heldByNode.get(id) ?? [];
      if (holders.length === 0) return { known: true as const, label: null };
      return {
        known: true as const,
        label: holders.map((h) => h.user_initials || h.agent_label || "an agent").join(" and "),
      };
    },
    [presence, heldByNode],
  );
  const clouds = React.useMemo(() => cloudsFor(presence, pos), [presence, pos]);
  const [soloUser, setSoloUser] = React.useState<string | null>(null);
  const soloed = React.useMemo(() => nodesHeldBy(presence, soloUser), [presence, soloUser]);

  const degree = React.useMemo(() => degrees(ids, layoutEdges), [ids, layoutEdges]);
  const tabOrder = React.useMemo(
    () => [...ids].sort((a, b) => degree[b] - degree[a] || (a < b ? -1 : a > b ? 1 : 0)),
    [ids, degree],
  );
  const selectNode = React.useCallback(
    (id: string) => {
      // A node pick leaves the matrix pair. The return chip stays until Back or a fresh Matrix entry.
      setPairFocus(null);
      // Armed: the next pick is the far end of a path, not a new selection. Routing click,
      // Enter and the toolbar through here is what keeps the keyboard equal to the mouse.
      if (armed && selPath && id !== selPath) {
        setPathTo(id);
        setArmed(false);
        return;
      }
      setSelPath(id);
      setDepth(1);
      setPathTo(null);
      setTrail((prev) => {
        const deduped = prev.filter((p) => p !== id);
        return [...deduped.slice(-7), id];
      });
    },
    [armed, selPath],
  );

  // AC-18. Scoped to the edge types currently drawn: a ranking computed over edges the canvas
  // is not showing would quietly disagree with the picture beside it.
  const activeTypes = React.useMemo(() => EDGE_TYPES.filter((t) => enabled[t]), [enabled]);
  const { data: analysis, isLoading: hubsLoading } = useCodeAnalysis(
    { projectId: activeId, edgeTypes: activeTypes, limit: 10 },
    hubsOpen,
  );
  // Separate query from the hubs one: it is keyed on the endpoints and must not refetch the
  // whole ranking every time you trace a different pair.
  const { data: trace } = useCodeAnalysis(
    {
      projectId: activeId,
      edgeTypes: activeTypes,
      a: selPath ?? undefined,
      b: pathTo ?? undefined,
    },
    !!(selPath && pathTo),
  );
  const walk = pathTo ? (trace?.path ?? null) : null;

  const pathHl = React.useMemo(() => pathHighlight(walk, edges), [walk, edges]);

  /** What the canvas dims against: a live path wins, otherwise the reach ring. */
  const shown = pathHl ?? hl;

  // Canvas path mode is a different request from the SVG one: it has to abort.
  // Passing a null source outside canvas mode keeps this hook from tracing a
  // pair the SVG query is already tracing.
  const enableAllEdges = React.useCallback(() => {
    setEnabled({ imports: true, calls: true, owns: true, tested_by: true, references: true });
  }, []);
  const canvasPath = useCanvasPath({
    projectId: activeId,
    source: canvasMode ? selPath : null,
    edgeTypes: activeTypes,
    allEdgeTypes: EDGE_TYPES,
    onUseAllTypes: enableAllEdges,
  });
  const canvasPathRef = React.useRef(canvasPath);
  canvasPathRef.current = canvasPath;
  React.useEffect(() => {
    if (!canvasMode) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      // Innermost layer first: abort, then disarm, then the selection.
      const layer = canvasPathRef.current.escape();
      if (layer === "selection") {
        setSelPath(null);
        setPathTo(null);
        setArmed(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canvasMode]);

  // Isolate stands down when there is nothing to isolate against. Without this the button
  // stays pressed after the selection clears while hiding nothing — a control whose state
  // disagrees with the canvas. One effect rather than a call in each of the four clear paths
  // (keyboard Escape, inspector close, background click, trail clear).
  React.useEffect(() => {
    // Pair focus is a reach set with no selected node. Clearing isolate here
    // would undo Show in graph on the same commit that armed it.
    if (!shown && !pairFocus) setIsolate(false);
  }, [shown, pairFocus]);

  // A selection made anywhere — canvas, hub, find result, a grounding chip in the chat —
  // renders in the Inspect pane. Without this, clicking a node while the aside sits on Ask
  // lights the ring and shows nothing, which is the control-that-does-nothing shape.
  React.useEffect(() => {
    if (selPath) setSidePane("inspect");
  }, [selPath]);

  const kb = useGraphKeyboard({
    order: tabOrder,
    onSelect: selectNode,
    onClear: () => {
      setSelPath(null);
      setPathTo(null);
      setArmed(false);
    },
    onExpand: () => setDepth((d) => Math.min(4, d + 1)),
    setViewport: view.setViewport,
  });

  /**
   * Picking a hub: select it, then ease the viewport onto it — the same path the find box
   * takes, rather than a fourth way for this view to move.
   */
  const [pendingFocus, setPendingFocus] = React.useState<string | null>(null);
  const pickHub = React.useCallback(
    (path: string) => {
      selectNode(path);
      if (canvasMode) {
        setFrameId(path);
        return;
      }
      setPendingFocus(path);
    },
    [selectNode, canvasMode],
  );

  // Find is highlight-by-another-name: it feeds the same dim path as selection rather than
  // introducing a second visual language for "these are the interesting ones".
  // Precedence, most explicit first: a typed query beats a transient hover, which beats a
  // sticky solo, which beats a selection made earlier. Each is the user's most recent act of
  // intent at the moment it applies, and solo sits below hover so a soloed teammate does not
  // stop you reading the rest of the graph.
  const lit = React.useMemo(() => {
    if (find.active) return find.matches;
    if (hovered) return hovered;
    if (soloUser) return soloed;
    return shown?.hlNodes ?? null;
  }, [find.active, find.matches, hovered, soloUser, soloed, shown]);

  // Ease the viewport onto the hits, once per query — not on every keystroke's re-render.
  const fitRef = React.useRef(view.fitTo);
  fitRef.current = view.fitTo;
  const posRef = React.useRef(pos);
  posRef.current = pos;
  const matchKey = find.active ? [...find.matches].sort().join(",") : "";
  React.useEffect(() => {
    if (canvasMode || !matchKey) return;
    const points = matchKey.split(",").map((id) => posRef.current[id]).filter(Boolean);
    if (points.length) fitRef.current(points);
  }, [matchKey, canvasMode]);

  // Deferred rather than done in the click: the layout may not have placed the node
  // yet, so `pos[path]` is undefined for a frame or three.
  React.useEffect(() => {
    if (canvasMode || !pendingFocus) return;
    const at = posRef.current[pendingFocus];
    if (!at) return;
    fitRef.current([at]);
    setPendingFocus(null);
  }, [pendingFocus, pos, canvasMode]);

  // PRD-47 S1: a failure leaves `nodes` empty too, and the empty-state copy below is an
  // assertion that nothing has been *described*. Only claim it once the read answered.
  const canvasNodes = React.useMemo(
    () =>
      allIds.map((id) => {
        const n = nodeByPath[id];
        return { id, path: n?.path ?? id, kind: n?.kind ?? "file", name: n?.name ?? "" };
      }),
    [allIds, nodeByPath],
  );
  // The matrix and the canvas must name the same areas, or a cell and the hull
  // it frames are different partitions of one map.
  const scale = React.useMemo(() => {
    if (!canvasMode) return null;
    return areasAtScale(
      canvasNodes,
      allEdges.filter((e) => e.type === "owns"),
      keptDepth,
    );
  }, [canvasMode, canvasNodes, allEdges, keptDepth]);
  const matrixModel = React.useMemo(
    () => (scale ? buildMatrix(scale.areas, allEdges, activeTypes) : null),
    [scale, allEdges, activeTypes],
  );
  const selectedMatrix =
    layoutMode === "matrix" && matrixModel ? cellFromModel(matrixModel, matrixCell) : null;
  const jumpAreas = React.useMemo(() => {
    if (!canvasMode || !scale) return areas;
    return scale.areas.map((a) => ({
      id: a.id,
      label: a.caption ?? a.id,
      color: a.color,
      meta: `${a.count} nodes`,
    }));
  }, [canvasMode, scale, areas]);

  // A layout-chip entry drops the previous cell. Back is the only way to restore it.
  const enterMatrix = React.useCallback(() => {
    setMatrixCell(null);
    setReturnPoint(null);
    setPairFocus(null);
    setMatrixScroll(0);
    setFrameArea(null);
    setFrameId(null);
    setLayoutMode("matrix");
  }, []);
  const showPairInGraph = React.useCallback(() => {
    if (!matrixCell) return;
    const scroll = matrixScrollRef.current?.scrollTop ?? 0;
    setMatrixScroll(scroll);
    setReturnPoint({ row: matrixCell.row, col: matrixCell.col, scroll });
    setPairFocus({ src: matrixCell.row, dst: matrixCell.col });
    setIsolate(true);
    setSelPath(null);
    setFrameArea(null);
    setFrameId(null);
    setLayoutMode("clusters");
  }, [matrixCell]);
  const backToMatrix = React.useCallback(() => {
    if (!returnPoint) return;
    setMatrixCell({ row: returnPoint.row, col: returnPoint.col });
    setMatrixScroll(returnPoint.scroll);
    setPairFocus(null);
    setIsolate(false);
    setLayoutMode("matrix");
    setReturnPoint(null);
  }, [returnPoint]);
  const onLayoutChip = React.useCallback(
    (id: GraphLayoutId) => {
      // The Matrix chip is a fresh entry even when Matrix is already showing:
      // it drops a cell that Back to matrix just restored. Re-clicking a
      // drawing chip is a no-op so the camera is not reset in place.
      if (id === "matrix") {
        enterMatrix();
        return;
      }
      if (id === layoutMode) return;
      setPairFocus(null);
      setFrameArea(null);
      setLayoutMode(id);
    },
    [enterMatrix, layoutMode],
  );

  React.useEffect(() => {
    if (!linkToast) return;
    const timer = window.setTimeout(() => setLinkToast(null), 5000);
    return () => window.clearTimeout(timer);
  }, [linkToast]);

  // Check the mount-time link once the map has been read. An unknown sel or
  // cell is dropped here and named; a later refetch does not re-read the URL.
  React.useEffect(() => {
    if (urlReady || isLoading || map === undefined) return;
    const linked = urlSeed.current;
    if (!linked) {
      setUrlReady(true);
      return;
    }
    let missing = false;
    const selKnown = !!linked.sel && allIds.includes(linked.sel);
    if (linked.sel && !selKnown) {
      setSelPath(null);
      missing = true;
    } else if (selKnown && linked.sel) {
      // Camera is not in the URL. A linked node is framed; otherwise the
      // canvas fits on layout, and the SVG view resets to Fit.
      setFrameId(linked.sel);
      if (!canvasMode) setPendingFocus(linked.sel);
    } else if (!canvasMode) {
      view.reset();
    }
    const cellKnown = !!(linked.cell && matrixModel && cellFromModel(matrixModel, linked.cell));
    if (linked.cellPresent && !cellKnown) {
      setMatrixCell(null);
      missing = true;
    }
    if (missing) setLinkToast(LINKED_NODE_TOAST);
    setUrlReady(true);
    // `view.reset` is a stable callback. Listing `view` would re-run this on
    // every render, because the viewport object is rebuilt each time.
  }, [urlReady, isLoading, map, allIds, canvasMode, matrixModel]);

  React.useEffect(() => {
    if (!urlReady) return;
    replaceGraphUrl({ layout: layoutMode, sel: selPath, depth, iso: isolate, cell: matrixCell });
  }, [urlReady, layoutMode, selPath, depth, isolate, matrixCell]);

  // `truncated === false` means the server looked and the page is complete.
  // A missing flag is complete only when the returned list matches the total.
  const truncated =
    map?.truncated === true
      ? true
      : map?.truncated === false
        ? false
        : (map?.node_count ?? nodes.length) > nodes.length;

  const failed = mapQ.isError && map === undefined;
  const empty = !isLoading && !failed && nodes.length === 0;

  const purpose = `The codebase as agents described it — modules, files, and symbols with typed relations. ${map ? `${counts.drawn} nodes · ${counts.edges} edges${counts.undescribed ? ` · ${counts.undescribed} referenced but not yet described` : ""}.` : ""}`;

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-h-0 flex-1 flex-col">
        <PlaceHeader
          viewName="Code graph"
          purpose={purpose}
          action={
            <div className="flex flex-wrap items-center gap-1.5">
            {/* No find box and no "Reset view" here. The design's header carries Hubs and the
                edge chips and nothing else (`showFind: false`): find is a canvas overlay, and
                Fit in the control cluster is the same `view.reset` this button called. */}
            {pinsApi.pinCount > 0 && (
              <button
                onClick={pinsApi.clearPins}
                title="Release every pinned node"
                className="mr-1 rounded-lg border border-control bg-surface-2 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:border-control-hover hover:text-fg"
              >
                Unpin {pinsApi.pinCount}
              </button>
            )}
            {!canvasMode && isFiltered && (
              // Only offered under a filter: with every type on, the layout already reflects
              // exactly what is drawn and re-laying out would move nodes for no reason.
              <button
                onClick={() => relayout(edges.map((e) => ({ a: e.src, b: e.dst })))}
                disabled={pending}
                title="Recompute positions from the visible edges only"
                className="mr-1 inline-flex items-center gap-1.5 rounded-lg border border-control bg-surface-2 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:border-control-hover hover:text-fg disabled:opacity-50"
              >
                {pending ? "Laying out…" : "Re-layout to visible"}
              </button>
            )}
            <button
              onClick={() => {
                setHubsOpen((o) => !o);
                setSidePane("inspect");
              }}
              aria-pressed={hubsOpen}
              title="Rank nodes by what depends on them"
              className={cn(
                "mr-1 inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] transition-colors",
                hubsOpen
                  ? "border-control-hover bg-surface-3 text-fg"
                  : "border-control bg-surface-2 text-muted hover:border-control-hover hover:text-fg",
              )}
            >
              Hubs
            </button>
            {EDGE_TYPES.map((t) => (
              <button
                key={t}
                onClick={() => setEnabled((e) => ({ ...e, [t]: !e[t] }))}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] transition-colors",
                  enabled[t] ? "border-control-hover bg-surface-3 text-fg" : "border-control bg-surface-2 text-faint",
                )}
              >
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: EDGE_META[t].color, opacity: enabled[t] ? 1 : 0.35 }}
                />
                {EDGE_META[t].label}
              </button>
            ))}
            </div>
          }
        />

        <div
          className="relative min-h-0 flex-1 overflow-hidden"
          style={{
            // Build.dc.html canvas wash: a faint violet radial over the page ground, so the
            // dotted grid is not sitting on a flat black.
            background:
              "radial-gradient(1000px 640px at 50% 45%, rgba(167,139,250,.05), transparent 70%)",
          }}
        >
          {failed ? (
            <PlannerError message={FETCH_FAILED} onRetry={() => void mapQ.refetch()} />
          ) : isLoading ? (
            <div className="p-8 text-center text-[13px] text-muted">Loading graph…</div>
          ) : empty ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
              <div className="text-lead font-semibold text-fg-2">No code described yet</div>
              <p className="max-w-[420px] text-body leading-relaxed text-muted">
                {/* Naming the wrong reading, rather than trusting "yet" to imply it. An empty
                    graph over a large repository looks identical to a graph of a small one,
                    and the reassuring reading — "there is not much here" — is the wrong one. */}
                Empty because nothing has been described, not because this project has no
                structure.
              </p>
              <p className="max-w-[420px] text-body leading-relaxed text-muted">
                A coding agent populates this graph by calling the{" "}
                <span className="font-mono text-accent">describe_code</span> MCP tool as it works —
                upserting module/file/symbol nodes and their imports/calls/ownership edges. Ask a
                question on the right and the agent will tell you what it knows so far.
              </p>
            </div>
          ) : canvasMode && layoutMode === "matrix" ? (
            <AreaMatrix
              nodes={canvasNodes}
              edges={allEdges}
              enabledTypes={activeTypes}
              selected={matrixCell}
              onSelect={setMatrixCell}
              onLayout={onLayoutChip}
              restoreScroll={matrixScroll}
              scrollRef={matrixScrollRef}
              prevDepth={keptDepth}
            />
          ) : canvasMode ? (
            <CodeGraphCanvas
              nodes={canvasNodes}
              edges={allEdges}
              enabledTypes={activeTypes}
              isolate={isolate}
              selectedId={selPath}
              onSelect={(id) => {
                if (id) selectNode(id);
                else setSelPath(null);
              }}
              truncated={truncated}
              totalNodes={map?.node_count ?? nodes.length}
              frameId={frameId}
              controlRef={canvasControlRef}
              onZoom={onCanvasZoom}
              holderOf={holderOf}
              pathPending={canvasPath.pending}
              pathArmed={canvasPath.picking}
              onAbortPath={() => {
                // One layer: abort leaves the mode armed. The listbox owns
                // Escape while it is focused, so this must be the canvas path,
                // not the SVG flag the small map still uses.
                canvasPath.escape();
              }}
              onDisarmPath={() => {
                canvasPath.escape();
              }}
              layout={layoutMode === "matrix" ? "clusters" : layoutMode}
              onLayout={onLayoutChip}
              prevDepth={keptDepth}
              onDepth={onAreaDepth}
              pairFocus={pairFocus}
              returnTo={
                returnPoint
                  ? {
                      text: `Back to matrix · ${returnPoint.row}→${returnPoint.col}`,
                      onClick: backToMatrix,
                    }
                  : null
              }
              frameAreaId={frameArea?.id ?? null}
              frameAreaNonce={frameArea?.n ?? 0}
              pathState={{
                picking: canvasPath.picking,
                pending: canvasPath.pending,
                slow: canvasPath.slow,
                path: canvasPath.path,
              }}
              onPathTarget={canvasPath.pick}
              onPathEmpty={canvasPath.disarm}
              canvasPins={canvasPins}
              presence={presence}
              soloUser={soloUser}
            />
          ) : (
            <svg
              ref={view.svgRef}
              viewBox={`0 0 ${W} ${H}`}
              className={cn(
                "h-full w-full touch-none focus:outline-none",
                view.panning ? "cursor-grabbing" : "cursor-grab",
              )}
              role="application"
              tabIndex={0}
              aria-label={
                // Carries the same facts as the visible caption, including the undescribed
                // count — a11y parity is the point, and it was the caption that was wrong
                // (GRPH-479). `ids` is the whole map: nothing is hidden inside a component.
                `Code graph: ${ids.length} nodes, ${edges.length} of ${allEdges.length} edges shown` +
                (counts.undescribed
                  ? `, ${counts.undescribed} referenced but not yet described. `
                  : `. `) +
                `Arrow keys move between nodes, Enter selects, Shift+Enter widens by one ring, ` +
                `Shift+arrows pan, Escape clears.`
              }
              onKeyDown={kb.onKeyDown}
              onClick={() => setSelPath(null)}
              {...view.svgHandlers}
            >
              <defs>
                {EDGE_TYPES.map((t) => (
                  <marker
                    key={t}
                    id={`code-arrow-${t}`}
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M0,0 L10,5 L0,10 z" fill={EDGE_META[t].color} />
                  </marker>
                ))}
                <filter id="code-cloud-blur" x="-50%" y="-50%" width="200%" height="200%">
                  <feGaussianBlur stdDeviation="18" />
                </filter>
                {/* The canvas grid from Build.dc.html, to the design's numbers: 24px pitch,
                    r=1 dots at #1a2026 (no token — the hairlines are #1e242a and up, and a
                    grid that read as a hairline would compete with the panel borders). */}
                <pattern id="code-dots" width="24" height="24" patternUnits="userSpaceOnUse">
                  <circle cx="1.5" cy="1.5" r="1" fill="#1a2026" />
                </pattern>
              </defs>

              <g
                transform={view.transform}
                style={{ transition: view.panning ? undefined : "transform 220ms ease" }}
              >
              {/* Inside the transform, so the grid pans and zooms with the scene and gives the
                  motion something to read against — outside it the canvas looks static. Sized
                  well past the viewBox so panning never reaches an edge. */}
              <rect
                x={-W * 2}
                y={-H * 2}
                width={W * 5}
                height={H * 5}
                fill="url(#code-dots)"
                pointerEvents="none"
              />
              {/* Presence clouds sit BENEATH the edges and never touch a node's own fill or
                  stroke. That is the load-bearing rule of the visual design: a held node must
                  still say what kind of node it is, and tinting the node would overload the one
                  channel that already carries meaning. */}
              {clouds.map((c, i) => (
                <circle
                  key={`cloud-${c.userId}-${i}`}
                  cx={c.cx}
                  cy={c.cy}
                  r={c.r}
                  fill={c.color}
                  // Solo fades the others rather than hiding them: "my teammate is here and
                  // three other people are elsewhere" is a different fact from "my teammate is
                  // the only person in the codebase", and only one of them is true.
                  fillOpacity={soloUser && c.userId !== soloUser ? 0.04 : 0.16}
                  filter="url(#code-cloud-blur)"
                  stroke={c.predicted ? c.color : undefined}
                  strokeOpacity={c.predicted ? 0.45 : undefined}
                  // A dashed edge says the area came from `predict_areas`, not declared
                  // touchpoints: the lease is real, but WHERE it lands is a guess.
                  strokeDasharray={c.predicted ? "6 5" : undefined}
                  style={{ pointerEvents: "none" }}
                />
              ))}

              {/* Directory hulls (Build.dc.html). Distinct from presence clouds (user colour,
                  blurred). Drawn only when clustered layout has actually grouped the members
                  — a hull on a force hairball is a costume. */}
              {hullGroups.map((g) => {
                  const pts = g.members.map((id) => pos[id]).filter(Boolean);
                  if (pts.length < 2) return null;
                  const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
                  const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
                  const r =
                    Math.max(...pts.map((p) => Math.hypot(p.x - cx, p.y - cy))) + 28;
                  const col = areaColor(g.id);
                  const go = () => pickHub(ids.includes(g.id) ? g.id : g.members[0]);
                  return (
                    <g
                      key={`area-hull-${g.id}`}
                      data-testid={`area-hull-${g.id}`}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        go();
                      }}
                      className="cursor-pointer"
                      opacity={0.95}
                    >
                      <circle
                        cx={cx}
                        cy={cy}
                        r={r}
                        fill={col}
                        fillOpacity={0.035}
                        stroke={col}
                        strokeOpacity={0.28}
                        strokeWidth={1}
                        strokeDasharray="2 6"
                      />
                      <text
                        x={cx}
                        y={cy - r - 6}
                        textAnchor="middle"
                        fontSize={10}
                        fontFamily="IBM Plex Sans, system-ui, sans-serif"
                        fill={col}
                        style={{ pointerEvents: "none" }}
                      >
                        {label(g.id, nodeByPath[g.id]?.name ?? "")}
                      </text>
                    </g>
                );
              })}

              {edges.map((e, i) => {
                const a = pos[e.src];
                const b = pos[e.dst];
                if (!a || !b) return null;
                const active = !shown || shown.hlEdges.has(String(i));
                // Isolate drops what dimming would merely fade (Build.dc.html).
                if (isolate && !active) return null;
                // Trim the endpoint back so the arrowhead sits at the node edge, not under it.
                const dx = b.x - a.x;
                const dy = b.y - a.y;
                const d = Math.hypot(dx, dy) || 1;
                const rb = kindRadius(nodeByPath[e.dst]?.kind ?? "", degree[e.dst] ?? 0);
                const bx = b.x - (dx / d) * (rb + 4);
                const by = b.y - (dy / d) * (rb + 4);
                return (
                  <line
                    key={i}
                    x1={a.x}
                    y1={a.y}
                    x2={bx}
                    y2={by}
                    stroke={EDGE_META[e.type].color}
                    strokeWidth={2}
                    strokeOpacity={active ? 0.65 : 0.1}
                    markerEnd={active ? `url(#code-arrow-${e.type})` : undefined}
                  />
                );
              })}

              {ids.map((id) => {
                const p = pos[id];
                if (!p) return null;
                const node = nodeByPath[id];
                const active = !lit || lit.has(id);
                // Isolate drops what dimming would merely fade (Build.dc.html).
                if (isolate && !active) return null;
                const meta = kindMeta(node?.kind ?? "");
                const areaId = membership[id] ?? id;
                const areaCol = areaColor(areaId);
                const described = !!node;
                const stale = described && !node.fresh;
                const pinned = pinsApi.isPinned(id);
                const focused = kb.focusId === id;
                const isHover = hoverId === id;
                const holders = heldByNode.get(id) ?? [];
                const contention = contentionOf(holders);
                const r = kindRadius(node?.kind ?? "", degree[id] ?? 0);
                const drawR = isHover ? r + 1.5 : r;
                // Level of detail: zoomed out, only the names worth the ink survive — the
                // selection, the search hits, and the hubs. Zoom past LABEL_ZOOM and the rest
                // arrive. Past ~40 nodes the labels were previously the densest ink on screen.
                const showLabel =
                  view.viewport.k > LABEL_ZOOM ||
                  selPath === id ||
                  focused ||
                  isHover ||
                  find.matches.has(id) ||
                  hubs.has(id);
                return (
                  <g
                    key={id}
                    transform={`translate(${p.x},${p.y})`}
                    className={cn(
                      "focus:outline-none",
                      pinned ? "cursor-grab" : "cursor-pointer",
                    )}
                    opacity={active ? 1 : 0.22}
                    role="button"
                    ref={kb.registerNode(id)}
                    tabIndex={NODE_TAB_INDEX}
                    aria-label={
                      `${meta.label} ${id}, ${degree[id] ?? 0} connections` +
                      (described ? (stale ? ", stale" : "") : ", not described") +
                      (pinned ? ", pinned" : "") +
                      (holders.length
                        ? `, held by ${holders.map((h) => h.user_initials || h.agent_label || "an agent").join(" and ")}`
                        : "")
                    }
                    aria-pressed={selPath === id}
                    onFocus={() => kb.setFocusId(id)}
                    onMouseEnter={() => setHoverId(id)}
                    onMouseLeave={() => setHoverId((h) => (h === id ? null : h))}
                    {...pinsApi.nodeHandlers(id)}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      // A drag ends with a click on the same node; do not also select it.
                      if (pinsApi.consumedDrag()) return;
                      // Alt-click traces a path from the current selection (AC-19). Kept as
                      // the PRD specified it, and NOT the only way in — see the Path control.
                      if (ev.altKey && selPath && selPath !== id) {
                        setPathTo(id);
                        setArmed(false);
                      }
                      // Shift-click widens the reach by a ring instead of starting over.
                      else if (ev.shiftKey && selPath === id) setDepth((d) => Math.min(4, d + 1));
                      else selectNode(id);
                    }}
                    onDoubleClick={(ev) => {
                      // Build.dc.html: double-click zooms onto the node. pickHub is the
                      // same path find and the inspector's Zoom to take.
                      ev.stopPropagation();
                      pickHub(id);
                    }}
                  >
                    {/* Hit circle — diamonds and squares are smaller than their clickable
                        neighbourhood. The design's hit path is max(r+6, 12). */}
                    <circle r={Math.max(drawR + 6, 12)} fill="transparent" />
                    <KindGlyph
                      kind={node?.kind ?? ""}
                      color={areaCol}
                      r={drawR}
                      described={described}
                      stale={stale}
                    />
                    {pinned && (
                      <circle r={drawR + 3} fill="none" stroke="#8b949e" strokeWidth={1} opacity={0.55} />
                    )}
                    {holders.length > 0 && (
                      <>
                        {/* Pulse ring in the HOLDER's colour — whose, not what kind. */}
                        <circle
                          className="hold-pulse"
                          r={drawR + 6}
                          fill="none"
                          stroke={holders[0].user_color ?? "#8b949e"}
                          strokeWidth={2}
                        />
                        {/* Role dot: what they are DOING to it is a different question from
                            whose they are, so it gets its own mark rather than tinting one. */}
                        <circle
                          cx={drawR + 4}
                          cy={-(drawR + 4)}
                          r={2.5}
                          fill={roleHex(holders[0].active_role)}
                          stroke="#0a0c0e"
                          strokeWidth={1}
                        />
                        {/* THE ALARM (D6). Two humans on one node means the partition failed,
                            and nothing else in the system raises an error when it does. The
                            colour blend of the overlapping clouds is what draws the eye; this
                            ring is what confirms it was not a trick of the blur. Binary on
                            purpose — three users is not "more contended", and encoding the
                            count here would add a fourth visual channel to a surface that
                            argues carefully for three. The number lives in the inspector. */}
                        {contention.contended && (
                          <circle
                            r={drawR + 10}
                            fill="none"
                            stroke="var(--color-st-blocked)"
                            strokeWidth={2}
                          />
                        )}
                      </>
                    )}
                    {/* One focus vocabulary: keyboard focus and selection wear the same ring. */}
                    {(selPath === id || focused) && (
                      <circle
                        r={drawR + 4}
                        fill="none"
                        stroke={areaCol}
                        strokeWidth={1.5}
                        opacity={focused ? 0.9 : 0.5}
                      />
                    )}
                    {showLabel && (
                      <text
                        x={drawR + 4}
                        y={4}
                        fontSize={11}
                        fontFamily="IBM Plex Mono, monospace"
                        fill="#8b949e"
                        // The label must not swallow the pointer, or a node becomes undraggable
                        // wherever its name happens to overlap a neighbour.
                        style={{ pointerEvents: "none" }}
                      >
                        {node ? label(id, node.name) : label(id, "")}
                      </text>
                    )}
                  </g>
                );
              })}

              {hoverId && hoverId !== selPath && pos[hoverId] && (
                <HoverTip
                  x={pos[hoverId].x}
                  y={pos[hoverId].y - kindRadius(nodeByPath[hoverId]?.kind ?? "", degree[hoverId] ?? 0) - 10}
                  title={label(hoverId, nodeByPath[hoverId]?.name ?? "")}
                  sub={truncate(nodeByPath[hoverId]?.summary || kindMeta(nodeByPath[hoverId]?.kind ?? "").label, 72)}
                />
              )}

              <ArrowsOut stubs={outbound} pos={pos} onHover={setStubHover} />
              </g>
            </svg>
          )}

          {/* Top-left, 320px, over the canvas — where the design puts find. The header's
              168px box is gone: a search whose results are nodes belongs on the surface
              that draws them, and the dropdown below it needs room the header never had. */}
          {!isLoading && !empty && (
            <CanvasFind
              find={find}
              nodeByPath={nodeByPath}
              onPick={pickHub}
              areas={areas}
              activeId={selPath}
            />
          )}

          {/* The design's bottom bar: legend on the left, controls on the right, one row,
              `justify-content:space-between`. Four overlays used to stack on top of each
              other in this corner — legend, trail, inspector and the stub card all claimed
              bottom-left — and the trail and inspector have moved to the aside, which is
              what makes a single row possible at all. */}
          {!isLoading && !empty && (
            <div
              data-testid="graph-bottom-bar"
              className="pointer-events-none absolute inset-x-4 bottom-3.5 z-10 flex flex-wrap-reverse items-end justify-between gap-2"
            >
              <div className="flex min-w-0 flex-col items-start gap-2">
                {stubHover && <StubEvidence stub={stubHover} />}
                <FleetLegend presence={presence} soloUser={soloUser} onSolo={setSoloUser} />
                <KindLegend kinds={drawnKinds} />
              </div>
              {layoutMode !== "matrix" && (
              <GraphControls
                depth={depth}
                onDepth={setDepth}
                isolate={isolate}
                onIsolate={() => setIsolate((v) => !v)}
                canIsolate={!!shown || !!pairFocus}
                zoom={canvasMode ? canvasK : view.viewport.k}
                minZoom={canvasMode ? CANVAS_MIN_K : MIN_ZOOM}
                maxZoom={canvasMode ? CANVAS_MAX_K : MAX_ZOOM}
                onZoom={(factor) => {
                  if (canvasMode) canvasControlRef.current?.zoomBy(factor);
                  else view.setViewport((v) => zoomAbout(v, factor, W / 2, H / 2));
                }}
                onFit={() => {
                  if (canvasMode) canvasControlRef.current?.fit();
                  else view.reset();
                }}
              />
              )}
            </div>
          )}
          {linkToast && (
            <div className="pointer-events-none absolute inset-x-0 bottom-16 z-20 flex justify-center">
              <p
                role="status"
                data-testid="graph-link-toast"
                className="rounded-lg border border-line bg-surface px-4 py-2 text-body text-fg shadow-lg"
              >
                {linkToast}
              </p>
            </div>
          )}
        </div>
      </div>

      {/* The design's 300px right aside. Hubs, the trail and the selection used to float on
          the canvas, where they covered the graph they describe and fought each other for
          the same corners. The design gives them a column instead and leaves the canvas to
          the drawing.

          The tabs are a divergence, recorded with the rest in PRD-47 §1.3. Build.dc.html has
          one aside and no chat on this page — but its own empty-state copy says "ask a
          question on the right", so the design took the app's words without the surface they
          name. Deleting a shipped feature is not matching a design, so the aside holds both
          and a node selection pulls Inspect forward, which is the case where the two would
          otherwise disagree. */}
      <aside
        data-testid="graph-aside"
        className="flex w-[300px] flex-none flex-col border-l border-line-2 bg-surface/60"
      >
        <div className="flex flex-none gap-1 border-b border-line-2 px-3 py-2">
          {(["inspect", "ask"] as const).map((pane) => (
            <button
              key={pane}
              onClick={() => setSidePane(pane)}
              aria-pressed={sidePane === pane}
              className={cn(
                "rounded-lg px-2.5 py-1 text-[11.5px] transition-colors",
                sidePane === pane
                  ? "bg-surface-3 text-fg"
                  : "text-muted hover:text-fg-2",
              )}
            >
              {pane === "inspect" ? "Inspect" : "Ask the codebase"}
            </button>
          ))}
        </div>

        {sidePane === "ask" ? (
          <CodeChat projectId={activeId} onSelectPath={setSelPath} />
        ) : (
          <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
            {hubsOpen && (
              <HubsPanel
                hubs={analysis?.hubs ?? []}
                sort={hubSort}
                onSort={setHubSort}
                onPick={pickHub}
                onClose={() => setHubsOpen(false)}
                selected={selPath}
                loading={hubsLoading}
                edgeTypes={activeTypes}
                allEdgeTypes={EDGE_TYPES}
                kindColour={(k) => kindMeta(k).color}
              />
            )}

            {canvasMode && (canvasPath.trace || canvasPath.requestError) ? (
              <PathTrace
                path={canvasPath.trace}
                onClear={canvasPath.clearTrace}
                onPick={pickHub}
                edgeTypes={activeTypes}
                allEdgeTypes={EDGE_TYPES}
                canvas
                requestError={canvasPath.requestError}
                onTryAll={canvasPath.tryAll}
                onRetry={canvasPath.retry}
              />
            ) : (
              walk && (
                <PathTrace
                  path={walk}
                  onClear={() => setPathTo(null)}
                  onPick={pickHub}
                  edgeTypes={activeTypes}
                  allEdgeTypes={EDGE_TYPES}
                />
              )
            )}

            {trail.length > 1 && (
              <TrailBreadcrumb
                trail={trail}
                nodeByPath={nodeByPath}
                onPick={(p) => {
                  setSelPath(p);
                  setDepth(1);
                  setPathTo(null);
                }}
                onClear={() => setTrail([])}
              />
            )}

            {layoutMode === "matrix" && selectedMatrix ? (
              <CellInspector
                cell={selectedMatrix}
                onShowInGraph={showPairInGraph}
                onJump={(src) => {
                  setMatrixCell(null);
                  setReturnPoint(null);
                  setPairFocus(null);
                  setFrameArea(null);
                  setLayoutMode("clusters");
                  pickHub(src);
                }}
              />
            ) : selPath ? (
              <NodeInspector
                path={selPath}
                nb={nb}
                depth={depth}
                reach={hl?.hlNodes.size ?? 1}
                holders={heldByNode.get(selPath) ?? []}
                servedAt={presence?.served_at ?? null}
                armed={canvasMode ? canvasPath.picking : armed}
                onArm={() => {
                  if (canvasMode) canvasPath.toggle();
                  else setArmed((a) => !a);
                }}
                onExpand={() => setDepth((d) => Math.min(4, d + 1))}
                onClose={() => setSelPath(null)}
                onZoom={() => pickHub(selPath)}
                onPick={pickHub}
                kind={nodeByPath[selPath]?.kind ?? ""}
                kindColor={kindMeta(nodeByPath[selPath]?.kind ?? "").color}
                areaId={membership[selPath] ?? selPath}
                areaLabel={label(
                  membership[selPath] ?? selPath,
                  nodeByPath[membership[selPath] ?? selPath]?.name ?? "",
                )}
                summary={nodeByPath[selPath]?.summary ?? ""}
              />
            ) : (
              /* The design's "Explore" state: with nothing selected, the aside teaches the
                 gestures and offers the areas to jump into. That is what this hint already
                 said from the canvas's bottom-right corner, so it moves rather than being
                 written twice — and the corner it vacates is the one the control cluster
                 needs. */
              !isLoading &&
              !empty &&
              !failed && (
                <InteractionHint
                  areas={jumpAreas}
                  onJump={(id) => {
                    if (canvasMode && scale?.areas.some((a) => a.id === id)) {
                      setLayoutMode("clusters");
                      setSelPath(null);
                      setPairFocus(null);
                      setReturnPoint(null);
                      setFrameId(null);
                      setFrameArea((cur) => ({ id, n: (cur?.n ?? 0) + 1 }));
                      return;
                    }
                    pickHub(id);
                  }}
                />
              )
            )}
          </div>
        )}
      </aside>
    </div>
  );
}

function NodeInspector({
  path,
  nb,
  depth,
  reach,
  holders,
  servedAt,
  armed,
  onArm,
  onExpand,
  onClose,
  onZoom,
  onPick,
  kind,
  kindColor: _kindColor,
  areaId,
  areaLabel,
  summary,
}: {
  path: string;
  nb: CodeNeighbors | null;
  depth: number;
  reach: number;
  holders: HeldArea[];
  servedAt: string | null;
  armed: boolean;
  onArm: () => void;
  onExpand: () => void;
  onClose: () => void;
  onZoom: () => void;
  onPick: (path: string) => void;
  kind: string;
  kindColor: string;
  areaId: string;
  areaLabel: string;
  summary: string;
}) {
  const contention = contentionOf(holders);
  const node = nb?.node ?? null;
  const meta = kindMeta(node?.kind || kind);
  const stale = node ? !node.fresh : false;
  const areaCol = areaColor(areaId);
  return (
    <div className="animate-fade">
      <div className="mb-1.5 flex items-center gap-2">
        <span
          className="rounded border border-line px-1.5 py-px font-mono text-meta tracking-wide text-muted"
        >
          {meta.label}
        </span>
        <span className="inline-flex items-center gap-1.5 font-mono text-[10px]" style={{ color: areaCol }}>
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: areaCol }} />
          {areaLabel}
        </span>
        {stale && (
          <span className="rounded border border-st-review/50 px-1.5 py-px font-mono text-[9px] uppercase tracking-wide text-st-review">
            stale
          </span>
        )}
        {!node && (
          <span className="font-mono text-[10px] text-faint">not described</span>
        )}
      </div>
      <div className="mb-2 break-all font-mono text-lead text-fg">
        {label(path, node?.name ?? "") || path}
      </div>
      {(node?.summary || summary || "").trim() ? (
        <p className="mb-2 text-small leading-relaxed text-muted">
          {(node?.summary || summary).trim()}
        </p>
      ) : null}

      {holders.length > 0 && servedAt && (
        <div
          className={cn(
            "mb-2.5 space-y-1 rounded-lg border p-2",
            contention.contended
              ? "border-st-blocked/50 bg-st-blocked/5"
              : "border-line-2 bg-surface-2/60",
          )}
        >
          {/* The inspector LEADS with the contention, because a person who clicked a ringed
              node is asking exactly one question and should not have to count rows to answer
              it. */}
          <div
            className={cn(
              "font-mono text-[10px] uppercase tracking-wide",
              contention.contended ? "text-st-blocked" : "text-faint",
            )}
          >
            {describeContention(contention)}
          </div>
          {holders.map((h, i) => {
            const left = secondsRemaining(h.expires_at, servedAt);
            return (
              <div key={`${h.agent_id}-${i}`} className="flex items-center gap-2 text-[11.5px]">
                <span
                  className="h-2 w-2 flex-none rounded-full"
                  style={{ background: h.user_color ?? "#8b949e" }}
                />
                <span className="font-mono text-[10px] text-fg-2">{h.user_initials || "??"}</span>
                <span className="min-w-0 flex-1 truncate text-muted">
                  {h.agent_label || h.agent_id}
                </span>
                <span
                  className="flex-none font-mono text-[9px] uppercase tracking-wide"
                  style={{ color: roleHex(h.active_role) }}
                >
                  {h.active_role}
                </span>
                {/* Time-remaining, not just a holder. An agent that dies mid-lease keeps its
                    glow until `expires_at` by design — correct, since the lease IS still held —
                    but it has to be legible, or this screen and the Fleet roster silently
                    disagree for the 450s where an agent reads offline and still holds. */}
                <span className="flex-none font-mono text-[9px] text-faint">
                  {formatRemaining(left)}
                </span>
              </div>
            );
          })}
          {holders.some((h) => h.predicted) && (
            <div className="font-mono text-[9px] uppercase tracking-wide text-faint">
              predicted area — the lease is real, where it lands is a guess
            </div>
          )}
        </div>
      )}

      <div className="mb-2 flex items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint">
          {depth} {depth === 1 ? "hop" : "hops"} · {reach} node{reach === 1 ? "" : "s"}
        </span>
        {depth < 4 && (
          <button
            onClick={onExpand}
            title="Widen the highlight by one ring (or Shift-click the node)"
            className="rounded border border-control px-1.5 py-px font-mono text-[10px] text-muted transition-colors hover:border-control-hover hover:text-fg"
          >
            expand +1
          </button>
        )}
      </div>
      {/* Build.dc.html: Zoom to / Clear / Path from here as one row. Clear keeps the
          accessible name the isolate stand-down test reaches ("Close the inspector") —
          a visible "Clear" that announced as nothing would be the × this replaces. */}
      <div className="mb-2 flex flex-wrap gap-1.5">
        <button
          onClick={onZoom}
          className="h-[26px] rounded-[7px] border border-control bg-surface-2 px-2.5 text-small text-fg-2 transition-colors hover:border-control-hover"
        >
          Zoom to
        </button>
        <button
          onClick={onClose}
          aria-label="Close the inspector"
          className="h-[26px] rounded-[7px] border border-control bg-transparent px-2.5 text-small text-muted transition-colors hover:text-fg"
        >
          Clear
        </button>
        <button
          onClick={onArm}
          aria-pressed={armed}
          title="Trace the shortest route from this node — or Alt-click the far end"
          className={cn(
            "h-[26px] rounded-[7px] border px-2.5 text-small transition-colors",
            armed
              ? "border-accent/60 bg-surface-3 text-accent"
              : "border-control bg-transparent text-muted hover:border-control-hover hover:text-fg",
          )}
        >
          {armed ? "Pick the far end…" : "Path from here"}
        </button>
      </div>
      {node?.summary && <p className="mb-3 text-body leading-relaxed text-muted">{node.summary}</p>}

      {!nb ? (
        <div className="font-mono text-[10px] text-faint">loading…</div>
      ) : (
        <div className="space-y-2.5">
          <EdgeList
            title="Depends on"
            rows={nb.outgoing.map((e) => ({ path: e.dst, type: e.type }))}
            onPick={onPick}
          />
          <EdgeList
            title="Used by"
            rows={nb.incoming.map((e) => ({ path: e.src, type: e.type }))}
            onPick={onPick}
          />

          {(nb.linked_items.length > 0 || nb.linked_requests.length > 0) && (
            <div>
              <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-faint">
                Linked work
              </div>
              <div className="space-y-1">
                {nb.linked_items.map((it) => (
                  <WorkRow key={`i-${it.id}`} id={it.id} title={it.title} relation={it.relation} color="#c6f24e" />
                ))}
                {nb.linked_requests.map((rq) => (
                  <WorkRow key={`r-${rq.id}`} id={rq.id} title={rq.title} relation={rq.relation} color="#c9b8ff" />
                ))}
              </div>
            </div>
          )}

          {nb.items_touching.length > 0 && (
            <div>
              <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-faint">
                Touching this <span className="text-faint-2">(by touchpoints)</span>
              </div>
              <div className="space-y-1">
                {nb.items_touching.map((it) => (
                  <div key={it.id} className="flex items-center gap-2 text-[12px]">
                    <span className="font-mono text-[10px] text-accent">{it.id}</span>
                    <span className="min-w-0 flex-1 truncate text-muted">{it.title}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function WorkRow({ id, title, relation, color }: { id: string; title: string; relation: string; color: string }) {
  return (
    <div className="flex items-center gap-2 text-[12px]">
      <span className="font-mono text-[10px]" style={{ color }}>{id}</span>
      <span className="min-w-0 flex-1 truncate text-muted">{title}</span>
      <span className="flex-none font-mono text-[9px] uppercase tracking-wide text-faint">{relation}</span>
    </div>
  );
}

function EdgeList({
  title,
  rows,
  onPick,
}: {
  title: string;
  rows: { path: string; type: CodeEdgeType }[];
  onPick: (path: string) => void;
}) {
  const empty = rows.length === 0;
  return (
    <div>
      <div className="mb-1.5 flex items-baseline gap-1.5">
        <span className="font-mono text-meta tracking-wide text-muted">{title}</span>
        <span className="font-mono text-meta text-faint-2">{rows.length}</span>
      </div>
      <div className="flex flex-col gap-0.5">
        {empty ? (
          <div className="flex items-center gap-2 px-1.5 py-1 font-mono text-small text-faint">
            <span className="h-1.5 w-1.5 flex-none rounded-full bg-line-3" />
            nothing
          </div>
        ) : (
          rows.map((r, i) => (
            <button
              key={i}
              onClick={() => onPick(r.path)}
              className="-mx-1.5 flex items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-surface-3"
            >
              <span className="h-1.5 w-1.5 flex-none rounded-full" style={{ background: EDGE_META[r.type].color }} />
              <span className="min-w-0 flex-1 truncate font-mono text-small text-fg-2">{r.path}</span>
              <span className="flex-none font-mono text-meta" style={{ color: EDGE_META[r.type].color }}>
                {EDGE_META[r.type].label}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/**
 * The arrows out (PRD-21 D4).
 *
 * A dependency that leaves the repo is drawn as a project stub — a square, so it is not
 * mistakable for a file — attached by a dashed line to the **real node for the file that
 * declares it**. That anchoring is the payoff for D3 refusing edges without evidence:
 * because the edge had to name `web/package.json`, this arrow can start there instead of
 * floating somewhere plausible, and every arrow is explainable by opening one file.
 *
 * A stub whose declaring file was never described still renders, in a tray at the edge,
 * saying why it has no anchor. Hiding it would lose a real dependency; drawing it from
 * nowhere with no explanation is the thing D3 exists to prevent.
 */
export function ArrowsOut({
  stubs,
  pos,
  onHover,
}: {
  stubs: ProjectStub[];
  pos: Record<string, { x: number; y: number }>;
  onHover: (s: ProjectStub | null) => void;
}) {
  if (stubs.length === 0) return null;
  let trayRow = 0;

  return (
    <g data-testid="arrows-out">
      {stubs.map((stub) => {
        const anchor = stub.anchor_paths.map((p) => pos[p]).find(Boolean);
        // Unanchored stubs stack down the left edge rather than overlapping the graph.
        const at = anchor
          ? { x: anchor.x + 90, y: anchor.y - 40 }
          : { x: 40, y: 60 + trayRow++ * 44 };

        return (
          <g
            key={stub.edge_id}
            data-testid={`stub-${stub.tag}`}
            opacity={stub.fresh ? 1 : 0.5}
            onPointerEnter={() => onHover(stub)}
            onPointerLeave={() => onHover(null)}
            className="cursor-pointer"
          >
            {anchor && (
              <line
                x1={anchor.x}
                y1={anchor.y}
                x2={at.x}
                y2={at.y}
                stroke={stub.accent}
                strokeOpacity={0.5}
                strokeWidth={1.5}
                strokeDasharray="5 4"
              />
            )}
            {/* Square, not a circle: a project is not a file, and the shapes should not
                have to be told apart by size alone. */}
            <rect
              x={at.x - 9}
              y={at.y - 9}
              width={18}
              height={18}
              rx={4}
              fill={`${stub.accent}22`}
              stroke={stub.accent}
              strokeWidth={1.5}
              strokeDasharray={stub.fresh ? undefined : "3 3"}
            />
            <text
              x={at.x + 14}
              y={at.y + 4}
              fontSize={11}
              fontFamily="IBM Plex Mono, monospace"
              fill={stub.accent}
              style={{ pointerEvents: "none" }}
            >
              {stub.tag}
              {stub.unanchored ? " ·  no anchor" : ""}
            </text>
          </g>
        );
      })}
    </g>
  );
}

/** The evidence for one arrow — the file, and the fact found in it. */
function StubEvidence({ stub }: { stub: ProjectStub }) {
  return (
    <div className="pointer-events-none max-w-[420px] rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5 shadow-xl">
      <div className="font-mono text-[10px] uppercase tracking-[0.06em] text-faint-2">
        depends on {stub.tag}
        {!stub.fresh && <span className="ml-2 text-st-review">stale</span>}
      </div>
      {stub.evidence.map((e, i) => (
        <div key={i} className="mt-1 font-mono text-small text-fg-2">
          {e.file} <span className="text-muted">→ {e.fact}</span>
        </div>
      ))}
      {stub.unanchored && (
        <p className="mt-1.5 text-small leading-relaxed text-st-review/80">
          The manifest names that file, but nothing here has described it — so the arrow is
          real and has nowhere to attach. Describing the file gives it an anchor.
        </p>
      )}
    </div>
  );
}

// ── Canvas controls ───────────────────────────────────────────────────────────
// The top-right cluster from `Build.dc.html`: depth chips, Isolate, and a zoom
// group of − / readout / + / Fit. Three of those five had no control at all —
// the view could only be zoomed by wheel and only reset by double-clicking the
// background, which is undiscoverable and unreachable from a keyboard.
//
// Sizes and the segmented shape are the design's, not rounded to the type scale:
// `index.css` says a size carrying no role stays arbitrary rather than being
// rounded into one, and 10/10.5/11.5/15px are what the design uses here. The
// zoom readout's #5c656e likewise has no token — --color-faint is #7f8891, two
// steps lighter — so it stays a literal rather than becoming a palette change,
// which PRD-47 §1.3 puts outside this PRD.

/** Zoom steps per click. Matches the wheel's feel without overshooting MIN/MAX. */
const ZOOM_STEP = 1.3;

// ── Kind legend ───────────────────────────────────────────────────────────────
// The design's bottom-left slot. Kind is SHAPE (ring / circle / diamond / square); colour
// still names the kind because the server has no cluster axis to colour by. The legend
// draws the same KindGlyph the canvas uses — a key of circles would explain nothing.
//
// Only the kinds actually drawn are listed. A fixed five-row key over a graph of nothing but
// files is a glossary, and it teaches the reader to ignore it.

function KindLegend({ kinds }: { kinds: string[] }) {
  if (kinds.length < 2) return null;
  return (
    <div className="pointer-events-auto flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[10px] border border-line-2 bg-surface/90 px-3 py-2 backdrop-blur-sm">
      {kinds.map((k) => {
        const meta = kindMeta(k);
        return (
          <span
            key={k}
            data-testid={`kind-legend-${k}`}
            className="inline-flex items-center gap-1.5 font-mono text-[10px] text-muted"
          >
            {/* colour is grey on purpose: fill on the canvas now names area */}
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <g transform="translate(7,7)">
                <KindGlyph kind={k} color={KIND_LEGEND_COLOR} r={kindShape(k) === "ring" ? 5.5 : 4.5} />
              </g>
            </svg>
            {meta.label}
          </span>
        );
      })}
    </div>
  );
}

// ── Canvas find ───────────────────────────────────────────────────────────────
// The design's top-left overlay: a 320px box with a magnifier, and under it the hits as a
// list. The header box this replaces could only ever show a COUNT — "7" beside the input —
// which tells you the search worked and not which seven, on a canvas where a hit may be a
// dot three screens away. The list names them and clicking one takes the same route a hub
// click does.

/** How many hits the dropdown names before it stops and says how many it did not. */
const FIND_ROWS = 8;

function CanvasFind({
  find,
  nodeByPath,
  onPick,
  areas,
  activeId,
}: {
  find: GraphFind;
  nodeByPath: Record<string, { name: string; kind: string }>;
  onPick: (path: string) => void;
  areas: { id: string; label: string; color: string; meta: string }[];
  activeId: string | null;
}) {
  const hits = React.useMemo(() => [...find.matches].sort(), [find.matches]);
  return (
    <div className="pointer-events-auto absolute left-4 top-3.5 z-10 flex w-[min(320px,calc(100%-2rem))] flex-col gap-2">
      <div className="relative">
        <svg
          width="13"
          height="13"
          viewBox="0 0 16 16"
          fill="none"
          aria-hidden="true"
          className="pointer-events-none absolute left-[11px] top-1/2 z-10 -translate-y-1/2 opacity-60"
        >
          <circle cx="7" cy="7" r="5" stroke="#8b949e" strokeWidth="1.6" />
          <path d="M11 11l3 3" stroke="#8b949e" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        <input
          ref={find.inputRef}
          value={find.query}
          onChange={(e) => find.setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && find.clear()}
          placeholder="Find a file, symbol or module…"
          aria-label="Find a node"
          className="h-[34px] w-full rounded-[10px] border border-control bg-surface/[0.92] pl-8 pr-3 text-body text-fg backdrop-blur-sm placeholder:text-faint focus:border-control-hover focus:outline-none"
        />
      </div>
      {find.active && (
        <div
          role="listbox"
          aria-label="Matching nodes"
          className="divide-y divide-line-2 overflow-hidden rounded-[10px] border border-line-2 bg-surface/[0.97] backdrop-blur-sm"
        >
          {/* A query that matched nothing says so. Rendering an empty box, or none at all,
              is indistinguishable from not having searched — the same absence-reads-as-clean
              rule `useGraphFind.active` exists to carry. */}
          {hits.length === 0 ? (
            <div className="px-3 py-2 text-small text-faint">No node matches that.</div>
          ) : (
            <>
              {hits.slice(0, FIND_ROWS).map((id) => {
                const node = nodeByPath[id];
                const meta = kindMeta(node?.kind ?? "");
                return (
                  <button
                    key={id}
                    role="option"
                    aria-selected={false}
                    onClick={() => onPick(id)}
                    className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-surface-2"
                  >
                    <span
                      className="h-[7px] w-[7px] flex-none rounded-full"
                      style={{ background: meta.color }}
                    />
                    <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-fg-2">
                      {label(id, node?.name ?? "")}
                    </span>
                    <span className="flex-none font-mono text-meta text-[#5c656e]">
                      {node ? meta.label.toLowerCase() : "undescribed"}
                    </span>
                  </button>
                );
              })}
              {hits.length > FIND_ROWS && (
                <div className="px-3 py-1.5 font-mono text-meta text-faint">
                  +{hits.length - FIND_ROWS} more lit on the canvas
                </div>
              )}
            </>
          )}
        </div>
      )}
      {areas.length >= 2 && (
        <div
          className="flex flex-nowrap gap-1.5 overflow-x-auto pb-0.5"
          style={{ scrollbarWidth: "none" }}
          aria-label="Areas"
        >
          {areas.map((a) => {
            const on = activeId === a.id;
            return (
              <button
                key={a.id}
                onClick={() => onPick(a.id)}
                aria-pressed={on}
                className={cn(
                  "inline-flex h-6 flex-none items-center gap-1.5 whitespace-nowrap rounded-xl border px-2.5 font-mono text-[10px] transition-colors",
                  on
                    ? "border-control-hover bg-surface-3 text-fg"
                    : "border-control bg-surface/[0.9] text-muted hover:border-control-hover",
                )}
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: a.color }} />
                {a.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function GraphControls({
  depth,
  onDepth,
  isolate,
  onIsolate,
  canIsolate,
  zoom,
  onZoom,
  onFit,
  minZoom = MIN_ZOOM,
  maxZoom = MAX_ZOOM,
}: {
  depth: number;
  onDepth: (d: number) => void;
  isolate: boolean;
  onIsolate: () => void;
  canIsolate: boolean;
  zoom: number;
  onZoom: (factor: number) => void;
  onFit: () => void;
  minZoom?: number;
  maxZoom?: number;
}) {
  // The design draws these segmented groups and their internal dividers at #1e242a, which is
  // --color-line-2 — a hairline, ~1.2:1. A control's border is its identity under WCAG 1.4.11,
  // so `control-borders.test.ts` holds controls on --color-control and the floor wins over the
  // drawing here (the same way GRPH-938's casing beat the design's). Recorded in PRD-47 §1.3.
  // The dividers live on the container as `divide-x` rather than as a border on each button,
  // so the separator is structure and no control carries its own hairline.
  const group =
    "flex divide-x divide-control overflow-hidden rounded-[9px] border border-control bg-surface/90 backdrop-blur-sm";
  const stepper =
    "flex h-[30px] w-8 items-center justify-center text-[15px] leading-none text-muted transition-colors hover:text-fg disabled:opacity-40 disabled:hover:text-muted";
  return (
    <div className="pointer-events-auto ml-auto flex flex-wrap items-center justify-end gap-1.5">
      {/* Each chip shows a bare "1"/"2"/"3", which announces as a digit and says nothing, so
          the accessible name is spelled out while the visible glyph stays as drawn. This repo
          treats accessible names as load-bearing — see graph-accessible-name.test.tsx. */}
      <div className={group} role="group" aria-label="Rings lit around the selection">
        {[1, 2, 3].map((d) => (
          <button
            key={d}
            onClick={() => onDepth(d)}
            aria-pressed={depth === d}
            aria-label={`Light ${d} ring${d > 1 ? "s" : ""} around the selection`}
            title={`Light ${d} ring${d > 1 ? "s" : ""} around the selection`}
            className={cn(
              "h-[30px] px-2.5 font-mono text-[10.5px] transition-colors",
              depth === d ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2",
            )}
          >
            {d === 1 ? "1 hop" : `${d} hops`}
          </button>
        ))}
      </div>
      <button
        onClick={onIsolate}
        aria-pressed={isolate}
        disabled={!canIsolate}
        title={
          canIsolate
            ? "Hide everything outside the lit rings instead of dimming it"
            : "Select a node first — isolate needs something to isolate against"
        }
        className={cn(
          "h-8 rounded-[9px] border px-[11px] text-[11.5px] transition-colors disabled:opacity-40",
          isolate
            ? "border-accent/45 bg-accent/10 text-accent"
            : "border-control bg-surface-2 text-muted enabled:hover:border-control-hover enabled:hover:text-fg",
        )}
      >
        Isolate
      </button>
      <div className={group}>
        <button
          onClick={() => onZoom(1 / ZOOM_STEP)}
          aria-label="Zoom out"
          disabled={zoom <= minZoom + 1e-6}
          className={stepper}
        >
          −
        </button>
        <span
          aria-live="polite"
          className="flex items-center px-1.5 font-mono text-[10px] text-[#5c656e]"
        >
          {Math.round(zoom * 100)}%
        </span>
        <button
          onClick={() => onZoom(ZOOM_STEP)}
          aria-label="Zoom in"
          disabled={zoom >= maxZoom - 1e-6}
          className={stepper}
        >
          +
        </button>
        <button
          onClick={onFit}
          className="h-[30px] px-2.5 text-[11.5px] text-muted transition-colors hover:text-fg"
        >
          Fit
        </button>
      </div>
    </div>
  );
}

// ── Trail breadcrumb ──────────────────────────────────────────────────────────
// The last N nodes the user selected, most-recent last. Clicking one re-selects
// it. A back button that does not erase history — the trail persists until the
// user clears it or starts a new path through the graph.

function TrailBreadcrumb({
  trail,
  nodeByPath,
  onPick,
  onClear,
}: {
  trail: string[];
  nodeByPath: Record<string, { name: string; kind: string }>;
  onPick: (path: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-1 gap-y-0.5">
      <span className="flex-none font-mono text-[9px] uppercase tracking-wide text-faint">Trail</span>
      {trail.map((p, i) => {
        const node = nodeByPath[p];
        const isLast = i === trail.length - 1;
        return (
          <React.Fragment key={p}>
            {i > 0 && <span className="flex-none text-faint-2">›</span>}
            <button
              onClick={() => onPick(p)}
              className={cn(
                "max-w-[140px] truncate rounded px-1.5 py-0.5 font-mono text-[10.5px] transition-colors",
                isLast
                  ? "bg-accent/10 text-accent"
                  : "text-muted hover:bg-surface-4 hover:text-fg",
              )}
              title={p}
            >
              {node ? label(p, node.name) : label(p, "")}
            </button>
          </React.Fragment>
        );
      })}
      <button
        onClick={onClear}
        className="ml-1 flex-none text-faint hover:text-fg"
        aria-label="Clear trail"
        title="Clear trail"
      >
        ×
      </button>
    </div>
  );
}

// ── Interaction hint ──────────────────────────────────────────────────────────
// The aside's no-selection state (the design calls it "Explore"): teaches the gestures and
// offers areas to jump into. It was a dismissible card in the canvas's bottom-right corner —
// the corner the design gives to the control cluster — and nothing needs dismissing once it
// is in a column of its own.

function InteractionHint({
  areas,
  onJump,
}: {
  areas: { id: string; label: string; color: string; meta: string }[];
  onJump: (id: string) => void;
}) {
  return (
    <div className="flex flex-col gap-2.5">
      <div className="text-[13px] font-semibold text-fg">Explore</div>
      <p className="text-[12px] leading-relaxed text-muted">
        Hover to preview a node&rsquo;s neighbourhood, click to pin it, double-click to zoom in.
        {areas.length > 0 ? " Or jump into an area:" : ""}
      </p>
      {areas.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {areas.map((a) => (
            <button
              key={a.id}
              onClick={() => onJump(a.id)}
              className="flex items-center gap-2.5 rounded-[9px] border border-control bg-surface-2 px-2.5 py-2 text-left transition-colors hover:border-control-hover"
            >
              <span className="h-2 w-2 flex-none rounded-full" style={{ background: a.color }} />
              <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-fg-2">
                {a.label}
              </span>
              {a.meta && <span className="font-mono text-[10px] text-muted">{a.meta}</span>}
            </button>
          ))}
        </div>
      )}
      <p className="text-[11.5px] leading-relaxed text-faint">
        Shift+click widens the highlight · Alt+click traces a path · drag pans, nodes pin.
      </p>
    </div>
  );
}
