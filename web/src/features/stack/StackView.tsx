import { Printer } from "lucide-react";
import * as React from "react";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import {
  FETCH_FAILED,
  PlannerError,
  CardGridSkeleton,
} from "@/components/planner/PlannerStates";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useStack, useStackHealth } from "@/lib/queries";
import { buildStackLayout, type Box, type EdgeRoute } from "@/lib/stack/layout";
import {
  LABEL_ZOOM,
  useGraphViewport,
  zoomAbout,
} from "@/lib/graph/useGraphViewport";
import type {
  ScanHealthState,
  StackComponentHealth,
  StackEdge,
  StackLiveness,
  StackNode,
  StackTopology,
} from "@/lib/types";

const HEALTH_SEV: Record<string, { label: string; className: string }> = {
  crit: { label: "CRIT", className: "border-rose-400 text-rose-300" },
  high: { label: "HIGH", className: "border-orange-400 text-orange-300" },
  med: { label: "MED", className: "border-amber-400 text-amber-200" },
  low: { label: "LOW", className: "border-line text-muted" },
};

const HEALTH_ROLLUP: Record<
  ScanHealthState,
  { square: string; text: string; band: string }
> = {
  clean_at_sha: {
    square: "bg-sky-400 border-sky-400",
    text: "text-sky-300",
    band: "bg-surface-2",
  },
  has_findings: {
    square: "bg-orange-400 border-orange-400",
    text: "text-orange-300",
    band: "bg-surface-2",
  },
  never_scanned: {
    square: "border-muted-2 bg-[repeating-linear-gradient(45deg,var(--muted-2,#5c656e)_0_1.5px,transparent_1.5px_3.5px)]",
    text: "text-muted",
    band: "bg-[repeating-linear-gradient(45deg,transparent_0_6px,rgba(139,148,158,0.13)_6px_7px)] bg-surface-2",
  },
  stale: {
    square: "border-muted-2 bg-[repeating-linear-gradient(45deg,var(--muted-2,#5c656e)_0_1.5px,transparent_1.5px_3.5px)]",
    text: "text-muted",
    band: "bg-[repeating-linear-gradient(45deg,transparent_0_6px,rgba(139,148,158,0.13)_6px_7px)] bg-surface-2",
  },
};

const LIVENESS_META: Record<
  StackLiveness,
  { label: string; className: string; dot: string; edge: string; dash: string }
> = {
  reachable: {
    label: "reachable",
    className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
    dot: "bg-emerald-400",
    edge: "#6b7681",
    dash: "",
  },
  unreachable: {
    label: "unreachable",
    className: "border-rose-500/40 bg-rose-500/10 text-rose-200",
    dot: "bg-rose-400",
    edge: "#ff6b6b",
    dash: "6 5",
  },
  unknown: {
    label: "unknown",
    className: "border-amber-500/40 bg-amber-500/10 text-amber-100",
    dot: "bg-amber-400",
    edge: "#9aa3ab",
    dash: "2 5",
  },
  not_configured: {
    label: "not configured",
    className: "border-line bg-surface-2 text-muted",
    dot: "bg-muted-2",
    edge: "#56616c",
    dash: "4 5",
  },
  uncalibrated: {
    label: "uncalibrated",
    className: "border-violet-500/40 bg-violet-500/10 text-violet-200",
    dot: "bg-violet-400",
    edge: "#c9b8ff",
    dash: "4 4",
  },
};

const RING_LABEL: Record<number, string> = {
  1: "Our code",
  2: "Infra we run",
  3: "External",
};

function liveMeta(l: StackLiveness) {
  return LIVENESS_META[l] ?? LIVENESS_META.unknown;
}

/**
 * Build → Stack: derived, read-only architecture map of the running system
 * (GRPH-1034 / PRD-P51 §2) with the code-health overlay (GRPH-1035 / §3). A
 * pan/zoom canvas of three nested ownership rings, host groups, deployable
 * boxes and drawn protocol-labelled edges — matching `docs/design/Stack.dc.html`.
 * Health badges never default to green: clean-at-sha / has-findings /
 * never-scanned-or-stale.
 */
export function StackView() {
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const q = useStack(activeId);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  // Design default: overlay on. Absence of scans still reads as NOT SCANNED, not clean.
  const [overlayOn, setOverlayOn] = React.useState(true);
  const healthQ = useStackHealth(activeId, overlayOn);

  const data = q.data;
  const health = healthQ.data ?? null;
  const healthById = React.useMemo(() => {
    const m: Record<string, StackComponentHealth> = {};
    for (const c of health?.components || []) m[c.id] = c;
    return m;
  }, [health]);
  const selected = data?.nodes.find((n) => n.id === selectedId) ?? null;
  const selectedHealth = selected ? healthById[selected.id] ?? null : null;

  React.useEffect(() => {
    if (!data) return;
    if (selectedId && !data.nodes.some((n) => n.id === selectedId)) {
      setSelectedId(null);
    }
  }, [data, selectedId]);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="stack-view">
      <PlaceHeader
        viewName={`Stack ${scope}`}
        purpose="Derived architecture of what runs, where, and what each piece depends on — deployables and externals, not modules."
        action={
          <div className="flex items-center gap-2 print:hidden">
            <button
              type="button"
              data-testid="stack-health-overlay"
              aria-pressed={overlayOn}
              onClick={() => setOverlayOn((v) => !v)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-[8px] border px-2.5 py-1.5 text-[12px] transition-colors",
                overlayOn
                  ? "border-control bg-hover text-fg"
                  : "border-control bg-transparent text-muted hover:border-control-hover",
              )}
            >
              <span
                className={cn(
                  "h-2.5 w-2.5 rounded-[2px] border-[1.5px] border-current",
                  overlayOn ? "bg-orange-400/80" : "bg-transparent",
                )}
              />
              Code health overlay
            </button>
            <button
              type="button"
              onClick={() => window.print()}
              className="inline-flex items-center gap-1.5 rounded-[8px] border border-control bg-surface-2 px-2.5 py-1.5 text-[12px] text-fg-2 transition-colors hover:border-control-hover"
            >
              <Printer size={14} />
              Print / PDF
            </button>
          </div>
        }
      />

      <div className="min-h-0 flex-1 overflow-hidden">
        {q.isError && !data ? (
          <div className="p-5">
            <PlannerError message={FETCH_FAILED} onRetry={() => void q.refetch()} />
          </div>
        ) : q.isLoading || !data ? (
          <div className="p-5">
            <CardGridSkeleton />
          </div>
        ) : (
          <div className="flex h-full min-h-0">
            <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
              <div className="flex-none px-5 pt-4 print:px-2">
                <IdentityStrip data={data} />
                <EmptiesBanner empties={data.empties} catalog={data.catalog} />
              </div>
              <StackCanvas
                data={data}
                selectedId={selectedId}
                onSelect={setSelectedId}
                overlayOn={overlayOn}
                healthById={healthById}
              />
            </div>
            {selected && (
              <Inspector
                node={selected}
                edges={data.edges}
                onSelect={setSelectedId}
                onClose={() => setSelectedId(null)}
                overlayOn={overlayOn}
                health={selectedHealth}
                healthLoading={overlayOn && healthQ.isLoading}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function IdentityStrip({ data }: { data: StackTopology }) {
  const id = data.identity;
  return (
    <div
      className="mb-3 flex flex-wrap gap-x-4 gap-y-1 rounded-[10px] border border-line bg-surface-2/60 px-3 py-2 font-mono text-small text-muted"
      data-testid="stack-identity"
    >
      <span>api {id.api_version} · {id.api_git_sha || "sha?"}</span>
      <span>web {id.web_bundle_sha || "unknown"}</span>
      <span>schema {id.schema_revision || "unknown"}</span>
      <span>db {id.db_engine || "unknown"}</span>
      <span>MCP {id.mcp_tools} tools</span>
      <span className="text-fg-2">
        project <span className="text-accent">{data.project_id}</span>
      </span>
    </div>
  );
}

function EmptiesBanner({
  empties,
  catalog,
}: {
  empties: StackTopology["empties"];
  catalog: StackTopology["catalog"];
}) {
  if (!empties.length && catalog.unwired_count === 0) return null;
  return (
    <div
      className="mb-3 flex flex-wrap gap-2"
      data-testid="stack-empties"
      role="status"
    >
      {empties.map((e) => (
        <span
          key={e.id}
          className="rounded-full border border-line bg-surface-2 px-2.5 py-1 text-small text-muted"
        >
          {e.message}
        </span>
      ))}
      {catalog.unwired_count > 0 && (
        <span className="rounded-full border border-line bg-surface-2 px-2.5 py-1 text-small text-muted">
          {catalog.unwired_count} catalog provider
          {catalog.unwired_count === 1 ? "" : "s"} unwired (not drawn)
        </span>
      )}
    </div>
  );
}

function StackCanvas({
  data,
  selectedId,
  onSelect,
  overlayOn,
  healthById,
}: {
  data: StackTopology;
  selectedId: string | null;
  onSelect: (id: string) => void;
  overlayOn: boolean;
  healthById: Record<string, StackComponentHealth>;
}) {
  const layout = React.useMemo(() => buildStackLayout(data), [data]);
  const { width: W, height: H } = layout;
  const view = useGraphViewport(W, H);
  const lod = view.viewport.k < LABEL_ZOOM;
  // Selecting a host highlights every node on it (design body line 50).
  const [hostSel, setHostSel] = React.useState<string | null>(null);
  const hostMembers = React.useMemo(() => {
    const m: Record<string, Set<string>> = {};
    for (const h of data.hosts) m[h.id] = new Set(h.nodes);
    return m;
  }, [data.hosts]);

  // Fit the whole map when the topology (and therefore the layout) changes.
  const fitRef = React.useRef(view.fitTo);
  fitRef.current = view.fitTo;
  React.useEffect(() => {
    if (layout.fitPoints.length) fitRef.current(layout.fitPoints);
  }, [layout]);

  // Print the whole map at a readable fit (acceptance: whole map, current theme).
  React.useEffect(() => {
    const before = () => view.fitTo(layout.fitPoints);
    window.addEventListener("beforeprint", before);
    return () => window.removeEventListener("beforeprint", before);
  }, [view, layout]);

  const zoomBy = (factor: number) =>
    view.setViewport((v) => zoomAbout(v, factor, W / 2, H / 2));
  const oneToOne = () =>
    view.setViewport((v) => zoomAbout(v, 1 / v.k, W / 2, H / 2));

  return (
    <div className="relative min-h-0 flex-1 overflow-hidden" data-testid="stack-canvas">
      <svg
        ref={view.svgRef}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        className={cn(
          "h-full w-full touch-none select-none",
          view.panning ? "cursor-grabbing" : "cursor-grab",
        )}
        onWheel={view.svgHandlers.onWheel}
        onPointerDown={view.svgHandlers.onPointerDown}
        onPointerMove={view.svgHandlers.onPointerMove}
        onPointerUp={view.svgHandlers.onPointerUp}
        onDoubleClick={view.svgHandlers.onDoubleClick}
      >
        <defs>
          <marker
            id="stack-arrow"
            viewBox="0 0 10 10"
            refX="8.5"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M0 0L10 5L0 10z" fill="context-stroke" />
          </marker>
        </defs>

        <g
          transform={view.transform}
          style={{ transition: view.panning ? undefined : "transform 220ms ease" }}
        >
          {/* Ownership rings — nested frames, outermost first. */}
          {layout.rings.map((r) => (
            <g key={r.ring}>
              <rect
                x={r.x}
                y={r.y}
                width={r.w}
                height={r.h}
                rx={18}
                fill="none"
                stroke="currentColor"
                className={
                  r.ring === 1
                    ? "text-accent/30"
                    : r.ring === 2
                      ? "text-violet-400/30"
                      : "text-muted-2/40"
                }
                strokeWidth={1}
                strokeDasharray="2 7"
              />
              <text
                x={r.x + 14}
                y={r.y + 18}
                className="fill-muted font-mono"
                style={{ fontSize: 11, letterSpacing: "0.12em" }}
              >
                {r.label}
              </text>
            </g>
          ))}

          {/* Host groups. Clicking the host label highlights its nodes. */}
          {layout.hostFrames.map((h) => {
            const on = hostSel === h.id;
            return (
              <g key={h.id} data-testid={`stack-host-${h.id}`}>
                <rect
                  x={h.x}
                  y={h.y}
                  width={h.w}
                  height={h.h}
                  rx={12}
                  fill="none"
                  stroke="currentColor"
                  className={on ? "text-accent/70" : "text-host/60"}
                  strokeWidth={on ? 1.5 : 1}
                  strokeDasharray="5 5"
                />
                <g
                  role="button"
                  aria-pressed={on}
                  className="cursor-pointer"
                  onClick={() => setHostSel(on ? null : h.id)}
                >
                  <rect
                    x={h.x + 6}
                    y={h.y - 17}
                    width={h.label.length * 6.4 + 14}
                    height={16}
                    rx={4}
                    fill="transparent"
                  />
                  <text
                    x={h.x + 12}
                    y={h.y - 6}
                    className={cn("font-mono uppercase", on ? "fill-accent" : "fill-muted")}
                    style={{ fontSize: 10, letterSpacing: "0.14em" }}
                  >
                    {h.label}
                  </text>
                </g>
              </g>
            );
          })}

          {/* Edges. */}
          {layout.edges.map((e, i) => (
            <EdgeLine key={`${e.from}-${e.to}-${i}`} edge={e} lod={lod} />
          ))}

          {/* Boxes. */}
          {data.nodes.map((n) => {
            const b = layout.boxes[n.id];
            if (!b) return null;
            const member = hostSel ? !!hostMembers[hostSel]?.has(n.id) : false;
            return (
              <foreignObject key={n.id} x={b.x} y={b.y} width={b.w} height={b.h}>
                <NodeBox
                  node={n}
                  box={b}
                  lod={lod}
                  selected={selectedId === n.id}
                  dim={!!hostSel && !member}
                  hostLit={member}
                  edgeCount={
                    data.edges.filter((ed) => ed.from === n.id || ed.to === n.id).length
                  }
                  onSelect={() => onSelect(n.id)}
                  health={overlayOn ? healthById[n.id] ?? null : null}
                />
              </foreignObject>
            );
          })}
        </g>
      </svg>

      <ZoomControls
        pct={Math.round(view.viewport.k * 100)}
        lod={lod}
        onIn={() => zoomBy(1.2)}
        onOut={() => zoomBy(1 / 1.2)}
        onFit={() => view.fitTo(layout.fitPoints)}
        onOneToOne={oneToOne}
      />
      <CanvasLegend nodes={data.nodes} overlayOn={overlayOn} />
    </div>
  );
}

function EdgeLine({ edge, lod }: { edge: EdgeRoute; lod: boolean }) {
  const m = liveMeta(edge.liveness);
  const d = edge.points
    .map((p, i) => `${i === 0 ? "M" : "L"}${p.x} ${p.y}`)
    .join(" ");
  const label = edge.label || edge.protocol;
  return (
    <g>
      <path
        d={d}
        fill="none"
        stroke={m.edge}
        strokeWidth={1.6}
        strokeDasharray={m.dash || undefined}
        strokeLinecap="round"
        strokeLinejoin="round"
        markerEnd="url(#stack-arrow)"
        data-edge={`${edge.from}->${edge.to}`}
        data-liveness={edge.liveness}
      />
      {!lod && label && (
        <g transform={`translate(${edge.mid.x}, ${edge.mid.y})`}>
          <rect
            x={-label.length * 3.2 - 5}
            y={-9}
            width={label.length * 6.4 + 10}
            height={16}
            rx={4}
            className="fill-surface stroke-line"
            strokeWidth={0.75}
          />
          <text
            textAnchor="middle"
            y={3}
            className="fill-muted font-mono"
            style={{ fontSize: 9.5 }}
          >
            {label}
          </text>
        </g>
      )}
    </g>
  );
}

function NodeBox({
  node,
  box,
  lod,
  selected,
  dim,
  hostLit,
  edgeCount,
  onSelect,
  health,
}: {
  node: StackNode;
  box: Box;
  lod: boolean;
  selected: boolean;
  dim: boolean;
  hostLit: boolean;
  edgeCount: number;
  onSelect: () => void;
  health: StackComponentHealth | null;
}) {
  const live = liveMeta(node.liveness);
  const surfacesAsTiles = node.surfaces.length >= 3;
  const roll = health?.rollup;
  const hStyle = roll ? HEALTH_ROLLUP[roll.state] : null;
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid={`stack-node-${node.id}`}
      aria-pressed={selected}
      data-host-lit={hostLit ? "" : undefined}
      data-health={roll?.state}
      style={{ width: box.w, height: box.h }}
      className={cn(
        "flex flex-col overflow-hidden rounded-[10px] border p-2.5 text-left transition-[opacity,border-color]",
        "bg-surface-2 hover:border-control-hover",
        selected || hostLit ? "border-accent ring-1 ring-accent/40" : "border-control",
        dim && "opacity-40",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[13px] font-medium text-fg">{node.label}</div>
          <div className="truncate text-[10px] text-muted">
            {RING_LABEL[node.ring] || node.kind}
          </div>
        </div>
        <span
          className={cn(
            "inline-flex flex-none items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]",
            live.className,
          )}
          title={live.label}
        >
          <span className={cn("h-1.5 w-1.5 rounded-full", live.dot)} />
          {!lod && live.label}
        </span>
      </div>

      {!lod && (
        <>
          {surfacesAsTiles ? (
            <div className="mt-2 grid grid-cols-2 gap-1">
              {node.surfaces.slice(0, 4).map((s) => (
                <span
                  key={s}
                  className="truncate rounded border border-line bg-surface px-1.5 py-1 text-[10px] text-fg-2"
                >
                  {s}
                </span>
              ))}
            </div>
          ) : (
            (node.surfaces.length > 0 || node.extensions.length > 0 || !!node.scope) && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                <ScopeChip scope={node.scope} />
                {node.extensions.map((ext) => (
                  <span
                    key={ext}
                    className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-accent"
                    title="Extension inside this box — not a peer"
                  >
                    {ext}
                  </span>
                ))}
                {node.surfaces.slice(0, 3).map((s) => (
                  <span
                    key={s}
                    className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted"
                  >
                    {s}
                  </span>
                ))}
              </div>
            )
          )}

          {surfacesAsTiles && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              <ScopeChip scope={node.scope} />
              {node.extensions.map((ext) => (
                <span
                  key={ext}
                  className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-accent"
                >
                  {ext}
                </span>
              ))}
            </div>
          )}

          {node.outposts && node.outposts.length > 0 && (
            <div className="mt-1.5 space-y-0.5">
              {node.outposts.map((o) => (
                <div key={o.host} className="font-mono text-[10px] text-muted-2">
                  {o.host} · {o.online}/{o.agents}
                </div>
              ))}
            </div>
          )}

          <div className="mt-auto pt-1.5 font-mono text-[10px] text-muted-2">
            {edgeCount} connection{edgeCount === 1 ? "" : "s"}
          </div>
        </>
      )}

      {roll && hStyle && (
        <div
          title="Code health — separate from liveness"
          data-testid={`stack-health-footer-${node.id}`}
          className={cn(
            "-mx-2.5 -mb-2.5 mt-auto flex items-center gap-1.5 border-t border-line px-2.5 py-1 font-mono text-meta",
            hStyle.band,
          )}
        >
          <span className="tracking-[0.06em] text-muted-2">CODE</span>
          <span className={cn("h-2 w-2 flex-none rounded-[2px] border-[1.5px]", hStyle.square)} />
          <span className={cn("font-semibold tracking-[0.04em]", hStyle.text)}>{roll.label}</span>
          {roll.severity_badges.map((b) => {
            const sev = HEALTH_SEV[b.severity] || HEALTH_SEV.low;
            return (
              <span
                key={b.severity}
                className={cn("rounded border px-1 text-[9px]", sev.className)}
              >
                {b.count} {sev.label}
              </span>
            );
          })}
          <span className="ml-auto truncate text-muted-2">{roll.meta}</span>
        </div>
      )}
    </button>
  );
}

function ScopeChip({ scope }: { scope: string }) {
  const project = scope === "project";
  return (
    <span
      className={cn(
        "rounded border px-1.5 py-0.5 text-[10px]",
        project
          ? "border-sky-500/30 bg-sky-500/10 text-sky-200"
          : "border-line text-muted",
      )}
      title={project ? "Resolves per project" : "Instance-wide"}
    >
      {project ? "project" : "instance"}
    </span>
  );
}

function ZoomControls({
  pct,
  lod,
  onIn,
  onOut,
  onFit,
  onOneToOne,
}: {
  pct: number;
  lod: boolean;
  onIn: () => void;
  onOut: () => void;
  onFit: () => void;
  onOneToOne: () => void;
}) {
  return (
    <div className="pointer-events-none absolute inset-x-4 bottom-3 flex flex-wrap items-end justify-end gap-2 print:hidden">
      {lod && (
        <span className="pointer-events-auto mr-auto rounded-md border border-line bg-surface-2/90 px-2 py-1 font-mono text-[10px] text-muted-2">
          detail hidden — zoom in to read
        </span>
      )}
      <div className="pointer-events-auto flex items-center overflow-hidden rounded-[9px] border border-line bg-surface-2/90">
        <button
          type="button"
          aria-label="Zoom out"
          onClick={onOut}
          className="h-8 w-8 text-[15px] text-muted hover:text-fg"
        >
          −
        </button>
        <span className="border-x border-line px-2 font-mono text-[10px] text-muted-2">
          {pct}%
        </span>
        <button
          type="button"
          aria-label="Zoom in"
          onClick={onIn}
          className="h-8 w-8 text-[15px] text-muted hover:text-fg"
        >
          +
        </button>
        <button
          type="button"
          onClick={onFit}
          className="h-8 border-l border-control px-2.5 text-[11.5px] text-muted hover:text-fg"
        >
          Fit
        </button>
        <button
          type="button"
          onClick={onOneToOne}
          className="h-8 border-l border-control px-2.5 font-mono text-small text-muted hover:text-fg"
        >
          1:1
        </button>
      </div>
    </div>
  );
}

function CanvasLegend({
  nodes,
  overlayOn,
}: {
  nodes: StackNode[];
  overlayOn: boolean;
}) {
  const seen = Array.from(new Set(nodes.map((n) => n.liveness)));
  return (
    <div
      className="pointer-events-none absolute bottom-3 left-4 flex flex-wrap items-center gap-3 rounded-[10px] border border-line bg-surface-2/90 px-3 py-2 text-small text-muted print:static print:mt-3"
      data-testid="stack-legend"
    >
      <span className="font-mono uppercase tracking-[0.06em] text-muted-2">Liveness</span>
      {(seen.length ? seen : (["reachable"] as StackLiveness[])).map((k) => {
        const m = liveMeta(k);
        return (
          <span key={k} className="inline-flex items-center gap-1.5">
            <span className={cn("h-1.5 w-1.5 rounded-full", m.dot)} />
            {m.label}
          </span>
        );
      })}
      {overlayOn && (
        <>
          <span className="font-mono uppercase tracking-[0.06em] text-muted-2">
            Code health ■ · our code only
          </span>
          <span className="inline-flex items-center gap-1.5 text-sky-300">
            <span className="h-2 w-2 rounded-[2px] bg-sky-400" />
            clean at head
          </span>
          <span className="inline-flex items-center gap-1.5 text-orange-300">
            <span className="h-2 w-2 rounded-[2px] bg-orange-400" />
            findings
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-[2px] border border-muted-2 bg-[repeating-linear-gradient(45deg,#5c656e_0_1.5px,transparent_1.5px_3.5px)]" />
            not scanned · stale
          </span>
        </>
      )}
      <span className="ml-1 font-mono uppercase tracking-[0.06em] text-muted-2">Scope</span>
      <span>instance · project</span>
    </div>
  );
}

function Inspector({
  node,
  edges,
  onSelect,
  onClose,
  overlayOn,
  health,
  healthLoading,
}: {
  node: StackNode;
  edges: StackEdge[];
  onSelect: (id: string) => void;
  onClose: () => void;
  overlayOn: boolean;
  health: StackComponentHealth | null;
  healthLoading: boolean;
}) {
  const live = liveMeta(node.liveness);
  const connections = edges.filter((e) => e.from === node.id || e.to === node.id);

  return (
    <aside
      className="flex w-[320px] flex-none flex-col border-l border-line bg-[rgba(9,11,13,0.7)] print:hidden"
      data-testid="stack-inspector"
    >
      <div className="flex items-start justify-between gap-2 border-b border-line px-4 py-3">
        <div>
          <div className="text-lead font-medium text-fg">{node.title || node.label}</div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <span className={cn("rounded-full border px-2 py-0.5 text-[10px]", live.className)}>
              {live.label}
            </span>
            <ScopeChip scope={node.scope} />
            <span className="rounded-full border border-line px-2 py-0.5 text-[10px] text-muted">
              {RING_LABEL[node.ring] || `ring ${node.ring}`}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-[12px] text-muted hover:text-fg-2"
          aria-label="Close inspector"
        >
          Close
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {node.extensions.length > 0 && (
          <Section title="Inside this box">
            {node.extensions.map((ext) => (
              <div key={ext} className="text-[12px] text-fg-2">
                <span className="font-mono text-accent">{ext}</span>
                <span className="text-muted"> — extension, not a peer deployable</span>
              </div>
            ))}
          </Section>
        )}

        {node.surfaces.length > 0 && (
          <Section title="Surfaces / ports">
            <div className="flex flex-wrap gap-1.5">
              {node.surfaces.map((s) => (
                <span
                  key={s}
                  className="rounded border border-control bg-surface-2 px-2 py-0.5 text-small text-fg-2"
                >
                  {s}
                </span>
              ))}
            </div>
          </Section>
        )}

        {node.outposts && node.outposts.length > 0 && (
          <Section title="Outposts">
            <ul className="space-y-1">
              {node.outposts.map((o) => (
                <li key={o.host} className="font-mono text-small text-fg-2">
                  {o.host}
                  <span className="text-muted">
                    {" "}· {o.online}/{o.agents} online
                  </span>
                </li>
              ))}
            </ul>
          </Section>
        )}

        {overlayOn && (
          <HealthInspector
            node={node}
            health={health}
            loading={healthLoading}
          />
        )}

        <Section title="Facts">
          {node.facts.length === 0 ? (
            <p className="text-[12px] text-muted">No facts reported.</p>
          ) : (
            <ul className="space-y-2">
              {node.facts.map((f) => (
                <li key={f.key} className="text-[12px]">
                  <div className="flex justify-between gap-2">
                    <span className="text-muted">{f.key}</span>
                    <span className="font-mono text-fg-2">{f.value}</span>
                  </div>
                  <div className="text-[10px] text-muted-2">{f.provenance}</div>
                </li>
              ))}
            </ul>
          )}
        </Section>

        <Section title="Connections">
          {connections.length === 0 ? (
            <p className="text-[12px] text-muted">No edges.</p>
          ) : (
            <ul className="space-y-1.5">
              {connections.map((e, i) => {
                const peer = e.from === node.id ? e.to : e.from;
                const outbound = e.from === node.id;
                return (
                  <li key={`${e.from}-${e.to}-${i}`}>
                    <button
                      type="button"
                      onClick={() => onSelect(peer)}
                      data-testid={`stack-conn-${peer}`}
                      className="flex w-full items-baseline gap-2 rounded border border-control bg-surface-2 px-2 py-1 text-left font-mono text-small text-fg-2 transition-colors hover:border-control-hover"
                      title={`Select ${peer}`}
                    >
                      <span className="text-accent">{outbound ? "→" : "←"}</span>
                      <span className="flex-1 truncate text-fg-2">{peer}</span>
                      <span className="text-muted-2">{e.protocol || e.label}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </Section>
      </div>
    </aside>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <h3 className="mb-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-muted">
        {title}
      </h3>
      {children}
    </div>
  );
}

function HealthInspector({
  node,
  health,
  loading,
}: {
  node: StackNode;
  health: StackComponentHealth | null;
  loading: boolean;
}) {
  // Infra / external / host-only: we don't scan code we don't own.
  if (!health) {
    const na =
      node.ring === 2
        ? "Infrastructure we run but didn't write, so we don't scan it. Liveness only."
        : node.ring === 3 || node.kind === "external"
          ? "An external service. We don't scan code we don't own. Liveness only."
          : node.kind === "agent"
            ? "Spawned agents are processes, not a deployable we scan."
            : loading
              ? "Loading code health…"
              : "No code-health data for this box.";
    return (
      <Section title="Code health · N/A">
        <p className="text-[12px] leading-snug text-muted" data-testid="stack-health-na">
          {na}
        </p>
      </Section>
    );
  }

  const roll = health.rollup;
  const hStyle = HEALTH_ROLLUP[roll.state];
  const cats = Object.entries(health.categories);

  return (
    <Section title="Code health">
      <div
        className="mb-2 rounded-[9px] border border-line bg-surface-2/80 p-2.5"
        data-testid="stack-health-summary"
      >
        <div className="flex items-center gap-2">
          <span className={cn("h-2.5 w-2.5 rounded-[2px] border-[1.5px]", hStyle.square)} />
          <span className={cn("font-mono text-small font-semibold tracking-[0.05em]", hStyle.text)}>
            {roll.label}
          </span>
          <span className="ml-auto font-mono text-[10px] text-muted-2">
            {health.path} {health.head_sha ? `@ ${health.head_sha.slice(0, 7)}` : ""}
          </span>
        </div>
        <p className="mt-1.5 text-[11.5px] leading-snug text-muted">
          No score. Each scanner reports on its own, with when it ran and against which
          revision. A scanner that hasn&apos;t run at head never counts as clean.
        </p>
      </div>

      <ul className="space-y-2" data-testid="stack-health-categories">
        {cats.map(([key, cat]) => {
          const st = cat.state;
          const stLabel =
            st === "clean_at_sha"
              ? "CLEAN"
              : st === "has_findings"
                ? `${cat.findings.length} FINDING${cat.findings.length === 1 ? "" : "S"}`
                : st === "stale"
                  ? "STALE"
                  : "NOT RUN";
          const stColor =
            st === "clean_at_sha"
              ? "text-sky-300 border-sky-400/50"
              : st === "has_findings"
                ? "text-orange-300 border-orange-400/50"
                : "text-muted border-host";
          return (
            <li
              key={key}
              className={cn(
                "rounded-[9px] border border-line p-2.5",
                st === "stale" || st === "never_scanned" ? HEALTH_ROLLUP.stale.band : "bg-surface-2",
              )}
            >
              <div className="flex items-center gap-2">
                <span className="text-[12px] font-medium text-fg">{cat.label}</span>
                <span
                  className={cn(
                    "rounded border px-1.5 py-0.5 font-mono text-meta tracking-[0.04em]",
                    stColor,
                  )}
                >
                  {stLabel}
                </span>
              </div>
              <div className="mt-1 font-mono text-[10px] text-muted-2">
                {cat.scanner || "no scanner"}
                {" · "}
                {st === "never_scanned"
                  ? "never run"
                  : cat.live
                    ? "live, reads the tracker"
                    : cat.scanned_at
                      ? `ran ${cat.scanned_at.slice(0, 16)}`
                      : "ran"}
              </div>
              <div className="font-mono text-[10px] text-muted-2">
                {st === "never_scanned"
                  ? "no result at any revision"
                  : cat.live
                    ? "not tied to a revision"
                    : cat.scanned_sha
                      ? `against ${cat.scanned_sha.slice(0, 12)}${
                          st === "stale" ? " · behind head" : health.head_sha ? " = head" : ""
                        }`
                      : ""}
              </div>
              {cat.note && (
                <p className="mt-1.5 text-[11.5px] leading-snug text-muted">{cat.note}</p>
              )}
              {cat.findings.length > 0 && (
                <ul className="mt-2 space-y-1.5">
                  {cat.findings.map((f, i) => {
                    const sev = HEALTH_SEV[f.severity] || HEALTH_SEV.low;
                    return (
                      <li
                        key={`${key}-${i}`}
                        className="flex items-start gap-2 text-[12px]"
                        data-testid="stack-health-finding"
                      >
                        <span
                          className={cn(
                            "mt-0.5 rounded border px-1 font-mono text-meta",
                            sev.className,
                          )}
                        >
                          {sev.label}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="leading-snug text-fg-2">{f.summary}</div>
                          <div className="font-mono text-[10px] text-muted-2">
                            {f.path}
                            {f.link_ref ? ` · ${f.link_ref}` : ""}
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </Section>
  );
}
