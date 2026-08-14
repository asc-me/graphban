import * as React from "react";

import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { degrees, topByDegree, withinHops } from "@/lib/graph/metrics";
import {
  cloudsFor,
  formatRemaining,
  indexByNode,
  roleHex,
  secondsRemaining,
} from "@/lib/graph/presence";
import { useGraphFind } from "@/lib/graph/useGraphFind";
import { useGraphKeyboard } from "@/lib/graph/useGraphKeyboard";
import { useGraphLayout } from "@/lib/graph/useGraphLayout";
import { useGraphPins } from "@/lib/graph/useGraphPins";
import { LABEL_ZOOM, useGraphViewport } from "@/lib/graph/useGraphViewport";
import { useCodeMap, useFleetPresence } from "@/lib/queries";
import type { CodeEdgeType, CodeNeighbors, HeldArea } from "@/lib/types";

import { CodeChat } from "./CodeChat";

const KIND_META: Record<string, { label: string; color: string }> = {
  module: { label: "Module", color: "#c6f24e" },
  file: { label: "File", color: "#7ca2ff" },
  symbol: { label: "Symbol", color: "#a78bfa" },
};
const kindMeta = (kind: string) => KIND_META[kind] ?? { label: kind || "node", color: "#8b949e" };

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
const R = 7;
/** How many hubs keep their name when zoomed out (PRD-20 D2, level of detail). */
const LOD_TOP_N = 12;

/** Short label for a node: its name, else the last path segment (after `/` or `::`). */
function label(path: string, name: string): string {
  if (name) return name;
  const seg = path.split("::").pop() ?? path;
  return seg.split("/").pop() ?? seg;
}

export function CodeGraphView() {
  const { activeId } = useProjectCtx();
  const { data: map, isLoading } = useCodeMap(activeId);
  const [enabled, setEnabled] = React.useState<Record<CodeEdgeType, boolean>>({
    imports: true, calls: true, owns: true, tested_by: true, references: true,
  });
  const [selPath, setSelPath] = React.useState<string | null>(null);
  const [nb, setNb] = React.useState<CodeNeighbors | null>(null);
  const [hoverId, setHoverId] = React.useState<string | null>(null);
  // How many rings the selection lights. Shift-click (or Shift+Enter) widens it; a fresh
  // selection resets it, so depth never quietly persists into the next thing you click.
  const [depth, setDepth] = React.useState(1);

  const nodes = map?.nodes ?? [];
  // The UNFILTERED set drives layout; the filtered one only decides what is drawn. That split
  // is what makes a chip toggle redraw instead of rearranging the map under the user (AC-3).
  const allEdges = React.useMemo(() => map?.edges ?? [], [map]);
  const edges = React.useMemo(() => allEdges.filter((e) => enabled[e.type]), [allEdges, enabled]);
  const isFiltered = EDGE_TYPES.some((t) => !enabled[t]);

  // Node ids are paths; include edge endpoints even if a node wasn't described (dangling).
  const ids = React.useMemo(() => {
    const s = new Set<string>();
    nodes.forEach((nd) => s.add(nd.path));
    allEdges.forEach((e) => {
      s.add(e.src);
      s.add(e.dst);
    });
    return [...s].sort();
  }, [nodes, allEdges]);

  const nodeByPath = React.useMemo(() => {
    const m: Record<string, (typeof nodes)[number]> = {};
    nodes.forEach((nd) => (m[nd.path] = nd));
    return m;
  }, [nodes]);

  const layoutEdges = React.useMemo(
    () => allEdges.map((e) => ({ a: e.src, b: e.dst })),
    [allEdges],
  );

  const view = useGraphViewport(W, H);
  const pinsApi = useGraphPins(view.toWorld);
  const { pos: laidOut, pending, relayout } = useGraphLayout(ids, layoutEdges, {
    width: W,
    height: H,
    pinned: pinsApi.pins,
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
  const find = useGraphFind(ids, labelOf);
  const hubs = React.useMemo(() => topByDegree(ids, layoutEdges, LOD_TOP_N), [ids, layoutEdges]);

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
  const heldByNode = React.useMemo(() => indexByNode(presence), [presence]);
  const clouds = React.useMemo(() => cloudsFor(presence, pos), [presence, pos]);

  const degree = React.useMemo(() => degrees(ids, layoutEdges), [ids, layoutEdges]);
  const tabOrder = React.useMemo(
    () => [...ids].sort((a, b) => degree[b] - degree[a] || (a < b ? -1 : a > b ? 1 : 0)),
    [ids, degree],
  );
  const selectNode = React.useCallback((id: string) => {
    setSelPath(id);
    setDepth(1);
  }, []);
  const kb = useGraphKeyboard({
    order: tabOrder,
    onSelect: selectNode,
    onClear: () => setSelPath(null),
    onExpand: () => setDepth((d) => Math.min(4, d + 1)),
    setViewport: view.setViewport,
  });

  // Find is highlight-by-another-name: it feeds the same dim path as selection rather than
  // introducing a second visual language for "these are the interesting ones".
  const lit = React.useMemo(() => {
    if (find.active) return find.matches;
    if (hovered) return hovered;
    return hl?.hlNodes ?? null;
  }, [find.active, find.matches, hovered, hl]);

  // Ease the viewport onto the hits, once per query — not on every keystroke's re-render.
  const fitRef = React.useRef(view.fitTo);
  fitRef.current = view.fitTo;
  const posRef = React.useRef(pos);
  posRef.current = pos;
  const matchKey = find.active ? [...find.matches].sort().join(",") : "";
  React.useEffect(() => {
    if (!matchKey) return;
    const points = matchKey.split(",").map((id) => posRef.current[id]).filter(Boolean);
    if (points.length) fitRef.current(points);
  }, [matchKey]);

  const empty = !isLoading && nodes.length === 0;

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex flex-none flex-wrap items-center gap-3 border-b border-line px-5 py-4">
          <div>
            <h1 className="text-[18px] font-semibold tracking-tight">Code graph</h1>
            <p className="mt-0.5 text-[12.5px] text-muted">
              The codebase as agents described it — modules, files, and symbols with typed
              relations. {map ? `${map.node_count} nodes · ${map.edge_count} edges.` : ""}
            </p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <div className="relative mr-1">
              <input
                ref={find.inputRef}
                value={find.query}
                onChange={(e) => find.setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && find.clear()}
                placeholder="Find  /"
                aria-label="Find a node"
                className="w-[168px] rounded-lg border border-line-2 bg-surface-2 px-2.5 py-1 text-[11.5px] text-fg placeholder:text-faint focus:border-line-hover focus:outline-none"
              />
              {find.active && (
                <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 font-mono text-[10px] text-faint">
                  {find.matches.size}
                </span>
              )}
            </div>
            {(view.viewport.k !== 1 || view.viewport.x !== 0 || view.viewport.y !== 0) && (
              <button
                onClick={view.reset}
                title="Reset the view (or double-click the background)"
                className="mr-1 rounded-lg border border-line-2 bg-surface-2 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:border-line-hover hover:text-fg"
              >
                Reset view
              </button>
            )}
            {pinsApi.pinCount > 0 && (
              <button
                onClick={pinsApi.clearPins}
                title="Release every pinned node"
                className="mr-1 rounded-lg border border-line-2 bg-surface-2 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:border-line-hover hover:text-fg"
              >
                Unpin {pinsApi.pinCount}
              </button>
            )}
            {isFiltered && (
              // Only offered under a filter: with every type on, the layout already reflects
              // exactly what is drawn and re-laying out would move nodes for no reason.
              <button
                onClick={() => relayout(edges.map((e) => ({ a: e.src, b: e.dst })))}
                disabled={pending}
                title="Recompute positions from the visible edges only"
                className="mr-1 inline-flex items-center gap-1.5 rounded-lg border border-line-2 bg-surface-2 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:border-line-hover hover:text-fg disabled:opacity-50"
              >
                {pending ? "Laying out…" : "Re-layout to visible"}
              </button>
            )}
            {EDGE_TYPES.map((t) => (
              <button
                key={t}
                onClick={() => setEnabled((e) => ({ ...e, [t]: !e[t] }))}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] transition-colors",
                  enabled[t] ? "border-line-hover bg-surface-3 text-fg" : "border-line-2 bg-surface-2 text-faint",
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
        </div>

        <div className="relative min-h-0 flex-1 overflow-hidden">
          {isLoading ? (
            <div className="p-8 text-center text-[13px] text-muted">Loading graph…</div>
          ) : empty ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
              <div className="text-[14px] font-semibold text-fg-2">No code described yet</div>
              <p className="max-w-[420px] text-[12.5px] leading-relaxed text-muted">
                A coding agent populates this graph by calling the{" "}
                <span className="font-mono text-accent">describe_code</span> MCP tool as it works —
                upserting module/file/symbol nodes and their imports/calls/ownership edges. Ask a
                question on the right and the agent will tell you what it knows so far.
              </p>
            </div>
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
                `Code graph: ${ids.length} nodes, ${edges.length} of ${allEdges.length} edges shown. ` +
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
              </defs>

              <g
                transform={view.transform}
                style={{ transition: view.panning ? undefined : "transform 220ms ease" }}
              >
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
                  fillOpacity={0.16}
                  filter="url(#code-cloud-blur)"
                  stroke={c.predicted ? c.color : undefined}
                  strokeOpacity={c.predicted ? 0.45 : undefined}
                  // A dashed edge says the area came from `predict_areas`, not declared
                  // touchpoints: the lease is real, but WHERE it lands is a guess.
                  strokeDasharray={c.predicted ? "6 5" : undefined}
                  style={{ pointerEvents: "none" }}
                />
              ))}

              {edges.map((e, i) => {
                const a = pos[e.src];
                const b = pos[e.dst];
                if (!a || !b) return null;
                const active = !hl || hl.hlEdges.has(String(i));
                // Trim the endpoint back so the arrowhead sits at the node edge, not under it.
                const dx = b.x - a.x;
                const dy = b.y - a.y;
                const d = Math.hypot(dx, dy) || 1;
                const bx = b.x - (dx / d) * (R + 4);
                const by = b.y - (dy / d) * (R + 4);
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
                const meta = kindMeta(node?.kind ?? "");
                const described = !!node;
                const stale = described && !node.fresh;
                const pinned = pinsApi.isPinned(id);
                const focused = kb.focusId === id;
                const isHover = hoverId === id;
                const holders = heldByNode.get(id) ?? [];
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
                    tabIndex={kb.tabIndexFor(id)}
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
                      // Shift-click widens the reach by a ring instead of starting over.
                      if (ev.shiftKey && selPath === id) setDepth((d) => Math.min(4, d + 1));
                      else selectNode(id);
                    }}
                  >
                    <circle
                      r={isHover ? R + 2 : R}
                      fill={described ? meta.color : "#0d1114"}
                      stroke={described ? "#0a0c0e" : meta.color}
                      strokeWidth={2}
                      strokeDasharray={!described || stale ? "2 2" : undefined}
                      opacity={described ? 1 : 0.7}
                    />
                    {pinned && (
                      <circle r={R + 3} fill="none" stroke="#8b949e" strokeWidth={1} opacity={0.55} />
                    )}
                    {holders.length > 0 && (
                      <>
                        {/* Pulse ring in the HOLDER's colour — whose, not what kind. */}
                        <circle
                          className="hold-pulse"
                          r={R + 6}
                          fill="none"
                          stroke={holders[0].user_color ?? "#8b949e"}
                          strokeWidth={2}
                        />
                        {/* Role dot: what they are DOING to it is a different question from
                            whose they are, so it gets its own mark rather than tinting one. */}
                        <circle
                          cx={R + 4}
                          cy={-(R + 4)}
                          r={2.5}
                          fill={roleHex(holders[0].active_role)}
                          stroke="#0a0c0e"
                          strokeWidth={1}
                        />
                      </>
                    )}
                    {/* One focus vocabulary: keyboard focus and selection wear the same ring. */}
                    {(selPath === id || focused) && (
                      <circle
                        r={R + 4}
                        fill="none"
                        stroke={meta.color}
                        strokeWidth={1.5}
                        opacity={focused ? 0.9 : 0.5}
                      />
                    )}
                    {showLabel && (
                      <text
                        x={11}
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
              </g>
            </svg>
          )}

          {selPath && (
            <NodeInspector
              path={selPath}
              nb={nb}
              depth={depth}
              reach={hl?.hlNodes.size ?? 1}
              holders={heldByNode.get(selPath) ?? []}
              servedAt={presence?.served_at ?? null}
              onExpand={() => setDepth((d) => Math.min(4, d + 1))}
              onClose={() => setSelPath(null)}
            />
          )}
        </div>
      </div>

      <aside className="flex w-[360px] flex-none flex-col border-l border-line bg-surface/50">
        <CodeChat projectId={activeId} onSelectPath={setSelPath} />
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
  onExpand,
  onClose,
}: {
  path: string;
  nb: CodeNeighbors | null;
  depth: number;
  reach: number;
  holders: HeldArea[];
  servedAt: string | null;
  onExpand: () => void;
  onClose: () => void;
}) {
  const node = nb?.node ?? null;
  const meta = kindMeta(node?.kind ?? "");
  const stale = node ? !node.fresh : false;
  return (
    <div className="absolute bottom-4 left-4 w-[340px] animate-fade rounded-[13px] border border-line-hover bg-surface-3/95 p-4 shadow-[0_20px_48px_rgba(0,0,0,0.5)]">
      <div className="mb-1.5 flex items-center gap-2">
        <span
          className="rounded border px-1.5 py-px font-mono text-[9px] uppercase tracking-wide"
          style={{ borderColor: meta.color, color: meta.color }}
        >
          {meta.label}
        </span>
        {stale && (
          <span className="rounded border border-st-review/50 px-1.5 py-px font-mono text-[9px] uppercase tracking-wide text-st-review">
            stale
          </span>
        )}
        {!node && (
          <span className="font-mono text-[10px] text-faint">not described</span>
        )}
        <button onClick={onClose} className="ml-auto text-faint hover:text-fg">
          ×
        </button>
      </div>
      <div className="mb-2 break-all font-mono text-[11.5px] text-fg-2">{path}</div>

      {holders.length > 0 && servedAt && (
        <div className="mb-2.5 space-y-1 rounded-lg border border-line-2 bg-surface-2/60 p-2">
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
            className="rounded border border-line-2 px-1.5 py-px font-mono text-[10px] text-muted transition-colors hover:border-line-hover hover:text-fg"
          >
            expand +1
          </button>
        )}
      </div>
      {node?.summary && <p className="mb-3 text-[12.5px] leading-relaxed text-muted">{node.summary}</p>}

      {!nb ? (
        <div className="font-mono text-[10px] text-faint">loading…</div>
      ) : (
        <div className="space-y-2.5">
          <EdgeList title="Depends on" rows={nb.outgoing.map((e) => ({ path: e.dst, type: e.type }))} />
          <EdgeList title="Used by" rows={nb.incoming.map((e) => ({ path: e.src, type: e.type }))} />

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

function EdgeList({ title, rows }: { title: string; rows: { path: string; type: CodeEdgeType }[] }) {
  if (rows.length === 0) return null;
  return (
    <div>
      <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-faint">{title}</div>
      <div className="space-y-1">
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-2 text-[12px]">
            <span className="h-1.5 w-1.5 flex-none rounded-full" style={{ background: EDGE_META[r.type].color }} />
            <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-muted">{r.path}</span>
            <span className="flex-none font-mono text-[9px] uppercase tracking-wide text-faint">
              {EDGE_META[r.type].label}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
