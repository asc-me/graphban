import { ListChecks, Printer } from "lucide-react";
import * as React from "react";
import { Link, useLocation } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import {
  FETCH_FAILED,
  PlannerError,
  CardGridSkeleton,
} from "@/components/planner/PlannerStates";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { projectPath, tagFromPath } from "@/lib/routes";
import { useFileFromStack, useStack, useStackHealth } from "@/lib/queries";
import {
  BUILT_WITH_GRAPHBAN,
  buildStackLayout,
  GRAPHBAN_KIND,
  isAppNode,
  OUTPOST_PREVIEW,
  PLACEHOLDER_KIND,
  prepareStackMap,
  SERVICES_NOT_MAPPED,
  type Box,
  type EdgeRoute,
  type StackLayoutOptions,
} from "@/lib/stack/layout";
import {
  LABEL_ZOOM,
  useGraphViewport,
  zoomAbout,
} from "@/lib/graph/useGraphViewport";
import type {
  Item,
  ScanHealthState,
  StackComponentHealth,
  StackScanCategory,
  StackScanCta,
  StackScanFinding,
  StackEdge,
  StackHealth,
  StackLiveness,
  StackMapping,
  StackNode,
  StackTopology,
} from "@/lib/types";

const HEALTH_SEV: Record<
  string,
  { label: string; className: string; text: string; square: string; border: string }
> = {
  crit: {
    label: "CRIT",
    className: "border-rose-400 text-rose-300",
    text: "text-rose-300",
    square: "bg-rose-400 border-rose-400",
    border: "border-rose-400/50",
  },
  high: {
    label: "HIGH",
    className: "border-orange-400 text-orange-300",
    text: "text-orange-300",
    square: "bg-orange-400 border-orange-400",
    border: "border-orange-400/50",
  },
  med: {
    label: "MED",
    className: "border-amber-400 text-amber-200",
    text: "text-amber-200",
    square: "bg-amber-400 border-amber-400",
    border: "border-amber-400/50",
  },
  low: {
    label: "LOW",
    className: "border-line text-muted",
    text: "text-muted",
    square: "bg-muted border-muted",
    border: "border-line",
  },
};

const SEV_ORDER = ["crit", "high", "med", "low"];

function sevMeta(severity: string | null | undefined) {
  return HEALTH_SEV[severity || ""] || HEALTH_SEV.low;
}

/** Worst severity among findings — the colour a FINDINGS cell takes (Stack.dc.html roll()). */
function worstSeverity(findings: { severity: string }[]): string | null {
  return SEV_ORDER.find((s) => findings.some((f) => (HEALTH_SEV[f.severity] ? f.severity : "low") === s)) ?? null;
}

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

/**
 * Rollup colours. FINDINGS takes the colour of the WORST severity present, not a fixed
 * orange: a box with one LOW and a box with a CRIT must not look the same (GRPH-1044).
 */
function rollupStyle(roll: StackComponentHealth["rollup"]) {
  const base = HEALTH_ROLLUP[roll.state];
  if (roll.state !== "has_findings") return base;
  const sev = sevMeta(roll.worst_severity);
  return { ...base, square: sev.square, text: sev.text };
}

/** Item bodies the overlay files. The `stack-filed:` tag is what marks the offer taken. */
function defragBody(
  health: StackComponentHealth,
  catKey: string,
  cat: StackScanCategory,
  f: StackScanFinding,
): Partial<Item> {
  const sev = sevMeta(f.severity).label;
  const where = f.path || health.path;
  return {
    title: `Defrag ${where}: ${f.summary}`.slice(0, 160),
    description:
      `Filed from the Stack code-health overlay (${health.id} · ${cat.label || catKey}).\n\n` +
      `${sev} finding from ${cat.scanner || "an unnamed scanner"}` +
      `${cat.scanned_sha ? ` at ${cat.scanned_sha.slice(0, 12)}` : ""}:\n\n> ${f.summary}\n\n` +
      `Path: ${where}. The summary is scanner output, not a verified diagnosis — confirm it first.`,
    tags: ["defrag", "tech-debt", f.filed_key as string],
    touchpoints: where ? [where] : [],
  };
}

function ctaBody(
  health: StackComponentHealth,
  catKey: string,
  cat: StackScanCategory,
  cta: StackScanCta,
): Partial<Item> {
  const label = cat.label || catKey;
  return cta.kind === "rerun"
    ? {
        title: `Re-run ${cat.scanner || label} on ${cta.path} at head`,
        description:
          `Filed from the Stack code-health overlay (${health.id} · ${label}). The last result ` +
          `is against ${cat.scanned_sha ? cat.scanned_sha.slice(0, 12) : "an unknown revision"}, ` +
          `which does not cover head${health.head_sha ? ` (${health.head_sha.slice(0, 12)})` : ""}. ` +
          `Re-run it and report with record_scan.`,
        tags: ["scan", cta.filed_key],
      }
    : {
        title: `Add a ${label} scanner for ${cta.path}`,
        description:
          `Filed from the Stack code-health overlay (${health.id} · ${label}). Nothing has ever ` +
          `scanned ${cta.path} for this, so its state is unknown, not clean. Pick a scanner, run ` +
          `it, and report with record_scan.`,
        tags: ["scan", cta.filed_key],
      };
}

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
  // GRPH-1049: the full scan report reads the same health, so fetch it whenever
  // the overlay OR the report wants it.
  const [reportOpen, setReportOpen] = React.useState(false);
  const healthQ = useStackHealth(activeId, overlayOn || reportOpen);

  const raw = q.data;
  // GRPH-1099: Graphban collapses to one box on a project that does not write it.
  // Expanding reads the same payload; it never makes another call.
  const [graphbanOpen, setGraphbanOpen] = React.useState(false);
  const map = React.useMemo(
    () => (raw ? prepareStackMap(raw, graphbanOpen) : null),
    [raw, graphbanOpen],
  );
  const data = map?.topo;
  const health = healthQ.data ?? null;
  const healthById = React.useMemo(() => {
    const m: Record<string, StackComponentHealth> = {};
    for (const c of health?.components || []) m[c.id] = c;
    return m;
  }, [health]);
  const selected = data?.nodes.find((n) => n.id === selectedId) ?? null;
  const selectedHealth = selected ? ownHealth(selected, healthById) : null;

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
            {map?.collapsible && (
              <button
                type="button"
                data-testid="stack-graphban-toggle"
                aria-pressed={graphbanOpen}
                onClick={() => setGraphbanOpen((v) => !v)}
                className="inline-flex items-center gap-1.5 rounded-[8px] border border-control bg-transparent px-2.5 py-1.5 text-[12px] text-muted transition-colors hover:border-control-hover"
              >
                {graphbanOpen ? "Collapse Graphban" : "Expand Graphban"}
              </button>
            )}
            <button
              type="button"
              data-testid="stack-report-toggle"
              aria-pressed={reportOpen}
              onClick={() => setReportOpen((v) => !v)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-[8px] border px-2.5 py-1.5 text-[12px] transition-colors",
                reportOpen
                  ? "border-accent bg-accent/10 text-fg"
                  : "border-control bg-transparent text-muted hover:border-control-hover",
              )}
            >
              <ListChecks size={14} />
              {reportOpen ? "Close report" : "Scan report"}
            </button>
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
        {q.isError && !raw ? (
          <div className="p-5">
            <PlannerError message={FETCH_FAILED} onRetry={() => void q.refetch()} />
          </div>
        ) : q.isLoading || !raw || !data || !map ? (
          <div className="p-5">
            <CardGridSkeleton />
          </div>
        ) : reportOpen ? (
          <StackReport
            health={health}
            loading={healthQ.isLoading}
            isError={healthQ.isError}
            onRetry={() => void healthQ.refetch()}
            onClose={() => setReportOpen(false)}
          />
        ) : (
          <div className="flex h-full min-h-0">
            <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
              <div className="flex-none px-5 pt-4 print:px-2">
                <IdentityStrip data={data} />
                <MappingBanner mapping={raw.mapping} />
                <EmptiesBanner empties={data.empties} catalog={data.catalog} />
              </div>
              <StackCanvas
                data={data}
                layoutOptions={map.layoutOptions}
                onToggleGraphban={map.collapsible ? () => setGraphbanOpen((v) => !v) : undefined}
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
                mapping={raw.mapping}
                nodes={raw.nodes}
                onExpand={
                  selected.kind === GRAPHBAN_KIND ? () => setGraphbanOpen(true) : undefined
                }
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

const short = (sha: string | null | undefined) => (sha ? sha.slice(0, 12) : "unknown");

/**
 * Freshness of the posted app map (GRPH-1099). Every state says something: no record
 * names the command that makes one, stale names both shas, and an unknown head never
 * reads as current. A missing block (older API) is the unmapped state, not silence.
 */
function MappingBanner({ mapping }: { mapping: StackMapping | undefined }) {
  const state = mapping?.state ?? "unmapped";
  const by =
    mapping?.producer_id
      ? ` · by ${mapping.producer_id}${mapping.producer_version ? ` ${mapping.producer_version}` : ""}`
      : "";
  let tone = "border-amber-500/40 bg-amber-500/10 text-amber-100";
  let body: React.ReactNode;
  if (state === "unmapped") {
    body = (
      <>
        No app topology record yet. These are the deployables the code map shows; the
        services they use are not mapped. Run <code className="font-mono">gban stack map</code>{" "}
        to map them.
      </>
    );
  } else if (state === "stale") {
    body = (
      <>
        App map is stale: mapped at{" "}
        <span className="font-mono" data-testid="stack-mapping-mapped">{short(mapping?.mapped_sha)}</span>,
        head is{" "}
        <span className="font-mono" data-testid="stack-mapping-head">{short(mapping?.head_sha)}</span>
        {by}. Run <code className="font-mono">gban stack map</code> to refresh.
      </>
    );
  } else if (state === "mapped") {
    tone = "border-line bg-surface-2/60 text-muted";
    body = (
      <>
        App map current at <span className="font-mono">{short(mapping?.mapped_sha)}</span>
        {by}.
      </>
    );
  } else {
    // freshness_unknown, or anything new: never reads as current.
    body = mapping?.mapped_sha ? (
      <>
        App map freshness unknown: mapped at{" "}
        <span className="font-mono">{short(mapping.mapped_sha)}</span>, and this API reports
        no head to compare it with{by}.
      </>
    ) : (
      <>App map freshness unknown: the app topology record could not be read.</>
    );
  }
  return (
    <div
      className={cn("mb-3 rounded-[10px] border px-3 py-2 text-small", tone)}
      data-testid="stack-mapping"
      data-state={state}
      role="status"
    >
      {body}
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
  layoutOptions,
  onToggleGraphban,
  selectedId,
  onSelect,
  overlayOn,
  healthById,
}: {
  data: StackTopology;
  layoutOptions?: StackLayoutOptions;
  onToggleGraphban?: () => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
  overlayOn: boolean;
  healthById: Record<string, StackComponentHealth>;
}) {
  const layout = React.useMemo(
    () => buildStackLayout(data, layoutOptions),
    [data, layoutOptions],
  );
  const { width: W, height: H } = layout;
  // The ring frames are filled and cover the canvas, so a background-only pan never starts.
  const view = useGraphViewport(W, H, { panAnywhere: true });
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
        onPointerCancel={view.svgHandlers.onPointerUp}
        onClickCapture={view.svgHandlers.onClickCapture}
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
          {/* Ownership rings — nested frames, outermost first, each with a faint
              tinted interior fill (the layered "cell shading" from the design) so
              the three bands read as nested zones, not bare outlines. Rings use a
              SOLID border; the dashed style is reserved for the physical host
              frames, so ownership (ring) reads differently from where-it-runs
              (host). The label
              sits just ABOVE each frame's top-left corner; because the frames
              cascade down-and-right as they nest, the three labels stagger into
              their own clear lines (and clear the host-frame labels below) instead
              of piling up over the canvas. SVG <text> takes its colour from `fill`,
              not `color`, so the label uses fill-* utilities — text-* rendered it as
              the default black on the dark canvas. */}
          {layout.rings.map((r) => {
            const stroke =
              r.ring === 1
                ? "text-accent/50"
                : r.ring === 2
                  ? "text-violet-400/50"
                  : "text-muted-2/55";
            const tint =
              r.ring === 1 ? "fill-accent" : r.ring === 2 ? "fill-violet-400" : "fill-muted-2";
            // The ring's ink: the fill for the badge number + title, and the
            // matching text-* (→ currentColor) for the badge circle's stroke.
            const inkFill =
              r.ring === 1 ? "fill-accent" : r.ring === 2 ? "fill-violet-300" : "fill-muted";
            const inkStroke =
              r.ring === 1 ? "text-accent" : r.ring === 2 ? "text-violet-300" : "text-muted";
            // The label reads as a circled number, an uppercase ownership title,
            // then a plain-language subtitle (Stack.dc.html). The subtitle sits
            // after the title; a mono glyph is ~0.6em wide, so title width is
            // ~0.66px per char at 11px incl. letter-spacing.
            const titleX = r.x + 24;
            const subX = titleX + r.title.length * 7.1 + 10;
            return (
              <g key={r.ring}>
                <rect
                  x={r.x}
                  y={r.y}
                  width={r.w}
                  height={r.h}
                  rx={18}
                  stroke="currentColor"
                  className={cn(tint, stroke)}
                  fillOpacity={0.05}
                  strokeWidth={r.ring === 3 ? 1 : 1.5}
                />
                <circle
                  cx={r.x + 11}
                  cy={r.y - 12}
                  r={8.5}
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.5}
                  className={inkStroke}
                />
                <text
                  x={r.x + 11}
                  y={r.y - 12}
                  textAnchor="middle"
                  dominantBaseline="central"
                  className={cn("font-mono font-semibold", inkFill)}
                  style={{ fontSize: 10 }}
                >
                  {r.num}
                </text>
                <text
                  x={titleX}
                  y={r.y - 7}
                  className={cn("font-mono font-semibold", inkFill)}
                  style={{ fontSize: 11, letterSpacing: "0.06em" }}
                >
                  {r.title}
                </text>
                <text
                  x={subX}
                  y={r.y - 7}
                  className="fill-muted-2"
                  style={{ fontSize: 10.5 }}
                >
                  {r.subtitle}
                </text>
              </g>
            );
          })}

          {/* The expanded instance's "Built with Graphban" lane. Its label collapses it. */}
          {layout.lane && (
            <g data-testid="stack-graphban-lane">
              <rect
                x={layout.lane.x}
                y={layout.lane.y}
                width={layout.lane.w}
                height={layout.lane.h}
                rx={14}
                fill="none"
                stroke="currentColor"
                className="text-violet-400/50"
                strokeWidth={1.5}
              />
              <g
                role="button"
                className="cursor-pointer"
                onClick={onToggleGraphban}
                data-testid="stack-graphban-lane-collapse"
              >
                <text
                  x={layout.lane.x + 12}
                  y={layout.lane.y + layout.lane.h + 16}
                  className="fill-violet-300 font-mono"
                  style={{ fontSize: 10.5, letterSpacing: "0.06em" }}
                >
                  {layout.lane.label.toUpperCase()} · collapse
                </text>
              </g>
            </g>
          )}

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
            if (n.kind === PLACEHOLDER_KIND) {
              return (
                <foreignObject key={n.id} x={b.x} y={b.y} width={b.w} height={b.h}>
                  <PlaceholderBox node={n} box={b} />
                </foreignObject>
              );
            }
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
                  health={overlayOn ? ownHealth(n, healthById) : null}
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

/**
 * Code health belongs to a box only where this project writes it (ring 1). Health is
 * keyed by repo path (`web/`, `backend/`), so on a project that merely runs Graphban
 * the `web` component is THAT project's web/ folder — drawing it on Graphban's web box
 * would credit one codebase's scans to another's deployable.
 */
function ownHealth(
  node: StackNode,
  healthById: Record<string, StackComponentHealth>,
): StackComponentHealth | null {
  return node.ring === 1 ? healthById[node.id] ?? null : null;
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
  const roll = health?.rollup;
  const hStyle = roll ? rollupStyle(roll) : null;
  const graphban = node.kind === GRAPHBAN_KIND;
  const unresolved = node.kind === "unresolved";
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid={`stack-node-${node.id}`}
      aria-pressed={selected}
      data-host-lit={hostLit ? "" : undefined}
      data-health={roll?.state}
      data-kind={node.kind}
      style={{ width: box.w, height: box.h }}
      className={cn(
        "flex flex-col overflow-hidden rounded-[10px] border p-2.5 text-left transition-[opacity,border-color]",
        "bg-surface-2 hover:border-control-hover",
        unresolved && "border-dashed",
        selected || hostLit
          ? "border-accent ring-1 ring-accent/40"
          : unresolved
            ? "border-amber-500/60"
            : graphban
              ? "border-violet-400/60"
              : "border-control",
        dim && "opacity-40",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-[13px] font-semibold text-fg">{node.label}</div>
          <div className="truncate text-[10px] text-muted">
            {graphban
              ? "the instance that builds this app"
              : unresolved
                ? `unresolved reference${node.provenance?.file ? ` · ${node.provenance.file}` : ""}`
                : RING_LABEL[node.ring] || node.kind}
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
          {(node.surfaces.length > 0 || node.extensions.length > 0 || !!node.scope) && (
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
              {node.surfaces.map((s) => (
                <span
                  key={s}
                  className="rounded border border-violet-400/40 px-1.5 py-0.5 font-mono text-[10px] text-violet-300"
                  title="Surface on this box — shares the process"
                >
                  {s}
                </span>
              ))}
            </div>
          )}

          {graphban && node.members && (
            <div className="mt-1.5 flex flex-wrap gap-1" data-testid="stack-graphban-members">
              {node.members.slice(0, 8).map((m) => (
                <span
                  key={m}
                  className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted"
                >
                  {m}
                </span>
              ))}
              {node.members.length > 8 && (
                <span className="font-mono text-[10px] text-muted-2">
                  +{node.members.length - 8} more
                </span>
              )}
            </div>
          )}

          {node.outposts && node.outposts.length > 0 && (
            <div className="mt-1.5 space-y-0.5" data-testid={`stack-outposts-${node.id}`}>
              {node.outposts.slice(0, OUTPOST_PREVIEW).map((o) => (
                <div key={o.host} className="font-mono text-[10px] text-muted-2">
                  {o.host} · {o.online}/{o.agents}
                </div>
              ))}
              {node.outposts.length > OUTPOST_PREVIEW && (
                <div className="font-mono text-[10px] text-muted-2">
                  +{node.outposts.length - OUTPOST_PREVIEW} more host
                  {node.outposts.length - OUTPOST_PREVIEW === 1 ? "" : "s"} — see inspector
                </div>
              )}
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
          data-worst={roll.worst_severity || ""}
          className={cn(
            "-mx-2.5 -mb-2.5 mt-auto flex items-center gap-1.5 border-t border-line px-2.5 py-1 font-mono text-meta",
            hStyle.band,
          )}
        >
          <span className="tracking-[0.06em] text-muted-2">CODE</span>
          <span className={cn("h-2 w-2 flex-none rounded-[2px] border-[1.5px]", hStyle.square)} />
          <span className={cn("font-semibold tracking-[0.04em]", hStyle.text)}>{roll.label}</span>
          {roll.severity_badges.map((b) => {
            const sev = sevMeta(b.severity);
            return (
              <span
                key={b.severity}
                data-testid={`stack-health-sev-${b.severity}`}
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

/** Ring 3 before any record: says the services are not mapped, never an empty frame. */
function PlaceholderBox({ node, box }: { node: StackNode; box: Box }) {
  return (
    <div
      data-testid={`stack-node-${node.id}`}
      data-kind={node.kind}
      style={{ width: box.w, height: box.h }}
      className="flex flex-col justify-center rounded-[10px] border border-dashed border-line px-3 text-left"
    >
      <div className="font-mono text-[12px] text-muted">{SERVICES_NOT_MAPPED}</div>
      <div className="mt-1 text-[10.5px] text-muted-2">
        run <code className="font-mono">gban stack map</code> to draw them
      </div>
    </div>
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
  mapping,
  nodes,
  onExpand,
  onSelect,
  onClose,
  overlayOn,
  health,
  healthLoading,
}: {
  node: StackNode;
  edges: StackEdge[];
  mapping?: StackMapping;
  /** Every box in the payload, so the collapsed box can list its members' liveness. */
  nodes?: StackNode[];
  onExpand?: () => void;
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
          <div className="font-mono text-lead font-semibold text-fg">{node.title || node.label}</div>
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
        {node.kind === GRAPHBAN_KIND && (
          <Section title={BUILT_WITH_GRAPHBAN}>
            <p className="mb-2 text-[12px] leading-snug text-muted">
              This project runs Graphban; it does not write it. The whole instance is one box.
            </p>
            {onExpand && (
              <button
                type="button"
                onClick={onExpand}
                data-testid="stack-graphban-expand"
                className="mb-2 rounded-[8px] border border-control px-2.5 py-1 text-[12px] text-fg-2 hover:border-control-hover"
              >
                Expand in place
              </button>
            )}
            <ul className="space-y-1">
              {(nodes ?? [])
                .filter((n) => node.members?.includes(n.id))
                .map((n) => {
                  const m = liveMeta(n.liveness);
                  return (
                    <li key={n.id} className="flex items-center gap-2 font-mono text-small text-fg-2">
                      <span className={cn("h-1.5 w-1.5 rounded-full", m.dot)} />
                      <span className="flex-1 truncate">{n.label}</span>
                      <span className="text-muted-2">{m.label}</span>
                    </li>
                  );
                })}
            </ul>
          </Section>
        )}

        {isAppNode(node) && node.provenance && (
          <ProvenanceSection node={node} mapping={mapping} />
        )}

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
          <Section title={`Outposts (${node.outposts.length})`}>
            <ul className="max-h-64 space-y-1 overflow-y-auto">
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

/**
 * Where a posted-record box came from: the file (a link into the Code graph), the
 * producer and version that read it, and the sha it was mapped at (GRPH-1099).
 */
function ProvenanceSection({ node, mapping }: { node: StackNode; mapping?: StackMapping }) {
  const { pathname } = useLocation();
  const tag = tagFromPath(pathname);
  const prov = node.provenance ?? {};
  const fact = (k: string) => node.facts.find((f) => f.key === k)?.value;
  const producer = mapping?.producer_id ?? fact("producer");
  const version = mapping?.producer_version ?? fact("producer_version");
  const sha = mapping?.mapped_sha ?? fact("mapped_sha");
  const sub = prov.file ? `code?sel=${encodeURIComponent(prov.file)}` : null;
  const href = sub ? (tag ? projectPath(tag, sub) : `/${sub}`) : null;
  return (
    <Section title="Provenance">
      <div className="space-y-1 text-[12px]" data-testid="stack-provenance">
        {href ? (
          <Link
            to={href}
            className="block truncate font-mono text-accent hover:underline"
            data-testid="stack-provenance-file"
          >
            {prov.file}
            {prov.line ? `:${prov.line}` : ""} ↗
          </Link>
        ) : (
          <div className="font-mono text-muted">file unknown</div>
        )}
        {prov.rule && <div className="font-mono text-small text-muted">rule {prov.rule}</div>}
        <div className="text-muted">
          by <span className="font-mono text-fg-2">{producer || "unknown"}</span>
          {version ? <span className="font-mono text-fg-2"> {version}</span> : null}
          {" "}at <span className="font-mono text-fg-2">{sha ? sha.slice(0, 12) : "unknown"}</span>
        </div>
      </div>
    </Section>
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

// ── GRPH-1049 · full scan report ────────────────────────────────────────────
// The inspector shows one box's health; this reads every component's scans at
// once so you can see the whole surface — filterable by component, category and
// severity. Honest by construction: the coverage matrix shows never-scanned and
// stale cells explicitly (absence must never read as clean), and the finding
// count is over what a scanner actually reported, never a score.

const CAT_ORDER = ["security", "refactor", "stub", "deadcode", "coverage", "tests", "work"];
const SEV_RANK: Record<string, number> = { crit: 0, high: 1, med: 2, low: 3 };

function normSev(s: string): string {
  const k = (s || "").toLowerCase();
  if (k === "critical") return "crit";
  if (k === "medium") return "med";
  if (k === "info") return "low";
  return k in HEALTH_SEV ? k : "low";
}

interface FlatFinding {
  component: string;
  componentPath: string;
  category: string;
  categoryLabel: string;
  sev: string;
  finding: StackScanFinding;
}

function StackReport({
  health,
  loading,
  isError,
  onRetry,
  onClose,
}: {
  health: StackHealth | null;
  loading: boolean;
  isError: boolean;
  onRetry: () => void;
  onClose: () => void;
}) {
  const findingHref = useFindingHref();
  const [fComp, setFComp] = React.useState<Set<string>>(new Set());
  const [fCat, setFCat] = React.useState<Set<string>>(new Set());
  const [fSev, setFSev] = React.useState<Set<string>>(new Set());
  const [query, setQuery] = React.useState("");

  const components = health?.components ?? [];

  // Every category key present, in the canonical order, then any extras.
  const catKeys = React.useMemo(() => {
    const present = new Set<string>();
    for (const c of components) for (const k of Object.keys(c.categories)) present.add(k);
    const ordered = CAT_ORDER.filter((k) => present.has(k));
    for (const k of present) if (!ordered.includes(k)) ordered.push(k);
    return ordered;
  }, [components]);
  const catLabel = React.useMemo(() => {
    const m: Record<string, string> = {};
    for (const c of components)
      for (const [k, cat] of Object.entries(c.categories)) m[k] = cat.label;
    return m;
  }, [components]);

  // Flatten every reported finding.
  const all = React.useMemo<FlatFinding[]>(() => {
    const rows: FlatFinding[] = [];
    for (const c of components) {
      for (const [key, cat] of Object.entries(c.categories)) {
        for (const f of cat.findings) {
          rows.push({
            component: c.id,
            componentPath: c.path,
            category: key,
            categoryLabel: cat.label,
            sev: normSev(f.severity),
            finding: f,
          });
        }
      }
    }
    return rows;
  }, [components]);

  const sevCounts = React.useMemo(() => {
    const m: Record<string, number> = { crit: 0, high: 0, med: 0, low: 0 };
    for (const r of all) m[r.sev] = (m[r.sev] ?? 0) + 1;
    return m;
  }, [all]);

  const shown = React.useMemo(() => {
    const ql = query.trim().toLowerCase();
    return all
      .filter((r) => (fComp.size ? fComp.has(r.component) : true))
      .filter((r) => (fCat.size ? fCat.has(r.category) : true))
      .filter((r) => (fSev.size ? fSev.has(r.sev) : true))
      .filter((r) =>
        ql
          ? r.finding.summary.toLowerCase().includes(ql) ||
            (r.finding.path || "").toLowerCase().includes(ql)
          : true,
      )
      .sort(
        (a, b) =>
          (SEV_RANK[a.sev] ?? 9) - (SEV_RANK[b.sev] ?? 9) ||
          a.component.localeCompare(b.component) ||
          a.category.localeCompare(b.category),
      );
  }, [all, fComp, fCat, fSev, query]);

  const filtered = fComp.size + fCat.size + fSev.size > 0 || query.trim().length > 0;
  const clearAll = () => {
    setFComp(new Set());
    setFCat(new Set());
    setFSev(new Set());
    setQuery("");
  };

  if (isError && !health) {
    return (
      <div className="p-5" data-testid="stack-report">
        <PlannerError message={FETCH_FAILED} onRetry={onRetry} />
      </div>
    );
  }
  if (!health && loading) {
    return (
      <div className="p-5" data-testid="stack-report">
        <CardGridSkeleton />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="stack-report">
      <div className="flex flex-none flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-5 py-3">
        <h2 className="text-lead font-medium text-fg">Scan report</h2>
        <span className="font-mono text-small text-muted-2">
          {health?.head_sha ? `head ${health.head_sha.slice(0, 7)}` : "head unknown"}
          {health?.generated_at ? ` · ${health.generated_at.slice(0, 16).replace("T", " ")}` : ""}
        </span>
        <span className="ml-auto font-mono text-small text-muted" data-testid="stack-report-count">
          {all.length} finding{all.length === 1 ? "" : "s"}
          {filtered ? ` · ${shown.length} shown` : ""}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="rounded-[8px] border border-control bg-surface-2 px-2.5 py-1 text-[12px] text-fg-2 hover:border-control-hover"
        >
          Close
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <p className="mb-3 max-w-prose text-[12px] leading-snug text-muted">
          Every scanner result at head, across all our code. No score: a scanner that
          hasn&apos;t run at head never counts as clean, and never-scanned or stale cells
          are shown as such, not hidden.
        </p>

        {/* Coverage matrix — honest per component × category. */}
        <div
          className="mb-4 overflow-x-auto rounded-[10px] border border-line"
          data-testid="stack-report-matrix"
        >
          <table className="w-full border-collapse text-left text-small">
            <thead>
              <tr className="bg-surface-2 text-muted-2">
                <th className="px-2.5 py-1.5 font-medium">component</th>
                {catKeys.map((k) => (
                  <th key={k} className="px-2.5 py-1.5 font-medium capitalize">
                    {catLabel[k]?.split("·")[0]?.trim() || k}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {components.map((c) => (
                <tr key={c.id} className="border-t border-line">
                  <td className="px-2.5 py-1.5 font-mono text-fg-2">
                    {c.id}
                    <span className="ml-1 text-muted-2">{c.path}</span>
                  </td>
                  {catKeys.map((k) => (
                    <td key={k} className="px-2.5 py-1.5">
                      <MatrixCell cat={c.categories[k]} />
                    </td>
                  ))}
                </tr>
              ))}
              {components.length === 0 && (
                <tr>
                  <td
                    colSpan={catKeys.length + 1}
                    className="px-2.5 py-3 text-center text-muted"
                    data-testid="stack-report-empty"
                  >
                    Nothing has been scanned for this project yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Filters. */}
        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
          <FilterRow label="Severity">
            {SEV_ORDER.map((s) => (
              <FilterChip
                key={s}
                testid={`stack-report-filter-sev-${s}`}
                on={fSev.has(s)}
                onClick={() => setFSev((prev) => toggle(prev, s))}
                className={HEALTH_SEV[s]?.className}
              >
                {HEALTH_SEV[s]?.label || s} · {sevCounts[s] ?? 0}
              </FilterChip>
            ))}
          </FilterRow>
          <FilterRow label="Component">
            {components.map((c) => (
              <FilterChip
                key={c.id}
                testid={`stack-report-filter-comp-${c.id}`}
                on={fComp.has(c.id)}
                onClick={() => setFComp((prev) => toggle(prev, c.id))}
              >
                {c.id}
              </FilterChip>
            ))}
          </FilterRow>
          <FilterRow label="Category">
            {catKeys.map((k) => (
              <FilterChip
                key={k}
                testid={`stack-report-filter-cat-${k}`}
                on={fCat.has(k)}
                onClick={() => setFCat((prev) => toggle(prev, k))}
              >
                {k}
              </FilterChip>
            ))}
          </FilterRow>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="filter text…"
            data-testid="stack-report-search"
            className="h-7 w-40 rounded-[7px] border border-control bg-surface-2 px-2 text-[12px] text-fg-2 placeholder:text-muted-2 focus:border-control-hover focus:outline-none"
          />
          {filtered && (
            <button
              type="button"
              onClick={clearAll}
              className="text-small text-muted hover:text-fg-2"
            >
              clear
            </button>
          )}
        </div>

        {/* Findings. */}
        {shown.length === 0 ? (
          <p className="py-6 text-center text-[12px] text-muted" data-testid="stack-report-none">
            {all.length === 0
              ? "No findings reported — but check the matrix above for never-scanned cells."
              : "No findings match these filters."}
          </p>
        ) : (
          <ul className="space-y-1.5">
            {shown.map((r, i) => {
              const sev = HEALTH_SEV[r.sev] || HEALTH_SEV.low;
              const href = findingHref(r.finding.link_kind, r.finding.link_ref);
              return (
                <li
                  key={`${r.component}-${r.category}-${i}`}
                  className="flex items-start gap-2 rounded-[8px] border border-line bg-surface-2/60 px-2.5 py-1.5 text-[12px]"
                  data-testid="stack-report-finding"
                >
                  <span className={cn("mt-0.5 flex-none rounded border px-1 font-mono text-meta", sev.className)}>
                    {sev.label}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="leading-snug text-fg-2">{r.finding.summary}</div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 font-mono text-[10px] text-muted-2">
                      <span className="rounded border border-control px-1 text-muted">{r.component}</span>
                      <span className="rounded border border-control px-1 text-muted">{r.category}</span>
                      <span className="truncate">{r.finding.path}</span>
                      {href && r.finding.link_ref ? (
                        <Link
                          to={href}
                          className="text-accent hover:underline"
                          data-testid="stack-report-finding-link"
                        >
                          {r.finding.link_ref} ↗
                        </Link>
                      ) : null}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

function toggle(prev: Set<string>, v: string): Set<string> {
  const next = new Set(prev);
  if (next.has(v)) next.delete(v);
  else next.add(v);
  return next;
}

function MatrixCell({ cat }: { cat: StackScanCategory | undefined }) {
  if (!cat) return <span className="text-muted-2">·</span>;
  const n = cat.findings.length;
  if (cat.state === "has_findings") {
    return (
      <span className="font-mono text-orange-300" title={cat.note || undefined}>
        {n} finding{n === 1 ? "" : "s"}
      </span>
    );
  }
  if (cat.state === "clean_at_sha") {
    // A tests cell's note carries its passed/failed/skipped counts (GRPH-1061).
    return (
      <span className="font-mono text-sky-300" title={cat.note || undefined}>
        clean
      </span>
    );
  }
  if (cat.state === "stale") {
    return (
      <span className="font-mono text-muted" title={cat.note || undefined}>
        stale
      </span>
    );
  }
  return (
    <span className="font-mono text-muted-2" title={cat.note || undefined}>
      not run
    </span>
  );
}

function FilterRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-muted-2">{label}</span>
      {children}
    </div>
  );
}

function FilterChip({
  on,
  onClick,
  testid,
  className,
  children,
}: {
  on: boolean;
  onClick: () => void;
  testid: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      data-testid={testid}
      aria-pressed={on}
      onClick={onClick}
      className={cn(
        "rounded-full border px-2 py-0.5 font-mono text-[10px] transition-colors",
        on
          ? "border-accent bg-accent/10 text-fg"
          : cn("bg-transparent text-muted hover:border-control-hover", className || "border-control"),
      )}
    >
      {children}
    </button>
  );
}

/**
 * In-app destination for a finding's cross-reference, honouring the hosted
 * `/p/:tag/...` scope when the current path has a tag, and the flat route
 * otherwise. Unknown kinds get no link (the ref stays plain text).
 */
function useFindingHref(): (
  kind: string | null | undefined,
  ref: string | null | undefined,
) => string | null {
  const { pathname } = useLocation();
  const tag = tagFromPath(pathname);
  return (kind, ref) => {
    if (!ref) return null;
    const sub =
      kind === "prd"
        ? `prds/${encodeURIComponent(ref)}`
        : kind === "code"
          ? "code"
          : kind === "item"
            ? `tracker?select=${encodeURIComponent(ref)}`
            : null;
    if (!sub) return null;
    return tag ? projectPath(tag, sub) : `/${sub}`;
  };
}

function FiledLink({ id, href }: { id: string; href: string | null }) {
  const text = `filed ${id} ↗`;
  return href ? (
    <Link
      to={href}
      className="font-mono text-[10.5px] text-muted hover:text-accent"
      data-testid="stack-health-filed"
    >
      {text}
    </Link>
  ) : (
    <span className="font-mono text-[10.5px] text-muted" data-testid="stack-health-filed">
      {text}
    </span>
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
  const findingHref = useFindingHref();
  const { activeId } = useProjectCtx();
  const filing = useFileFromStack(activeId);
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
  const hStyle = rollupStyle(roll);
  const cats = Object.entries(health.categories);
  const unfiled = cats.flatMap(([key, cat]) =>
    cat.findings
      .filter((f) => f.filed_key && !f.filed_item)
      .map((f) => defragBody(health, key, cat, f)),
  );
  const file = (bodies: Partial<Item>[]) => {
    if (!filing.isPending) filing.mutate(bodies);
  };

  return (
    <Section title="Code health">
      <div className="mb-2 flex items-center gap-2">
        <span className="font-mono text-[10px] text-muted-2">
          {health.path} {health.head_sha ? `@ ${health.head_sha.slice(0, 7)}` : ""}
        </span>
        {unfiled.length > 1 && (
          <button
            type="button"
            onClick={() => file(unfiled)}
            disabled={filing.isPending}
            data-testid="stack-health-defrag-all"
            className="ml-auto h-6 rounded-md border border-accent/40 bg-accent/10 px-2 text-small font-semibold text-accent hover:border-accent disabled:opacity-50"
          >
            Defrag all {unfiled.length}
          </button>
        )}
      </div>
      <div
        className={cn("mb-2 rounded-[9px] border border-line p-2.5", hStyle.band)}
        data-testid="stack-health-summary"
        data-worst={roll.worst_severity || ""}
      >
        <div className="flex flex-wrap items-center gap-1.5 font-mono text-[10px]">
          <span className={cn("h-2.5 w-2.5 rounded-[2px] border-[1.5px]", hStyle.square)} />
          <span className={cn("text-small font-semibold tracking-[0.05em]", hStyle.text)}>
            {roll.label}
          </span>
          {roll.severity_badges.map((b) => {
            const sev = sevMeta(b.severity);
            return (
              <span key={b.severity} className={cn("rounded border px-1", sev.className)}>
                {b.count} {sev.label}
              </span>
            );
          })}
          <span className="ml-auto text-muted-2">{roll.meta}</span>
        </div>
      </div>
      <p className="mb-2 text-[11.5px] leading-snug text-muted">
        No score. Each scanner reports on its own, with when it ran and against which
        revision. A scanner that hasn&apos;t run at head never counts as clean.
      </p>
      {filing.isError && (
        <p className="mb-2 text-[11.5px] text-rose-300" data-testid="stack-health-file-error">
          Could not file it: {(filing.error as Error)?.message || "request failed"}
        </p>
      )}

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
          const worst = st === "has_findings" ? sevMeta(worstSeverity(cat.findings)) : null;
          const stColor =
            st === "clean_at_sha"
              ? "text-sky-300 border-sky-400/50"
              : worst
                ? cn(worst.text, worst.border)
                : "text-muted border-host";
          const cta = cat.cta;
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
                  data-testid={`stack-health-cat-${key}`}
                  className={cn(
                    "ml-auto rounded border px-1.5 py-0.5 font-mono text-meta tracking-[0.04em]",
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
                {cat.reported_by ? (
                  <span data-testid="stack-health-reported-by">
                    {" · reported by "}
                    {cat.reported_by}
                  </span>
                ) : null}
              </div>
              <div
                className={cn(
                  "font-mono text-[10px]",
                  st === "stale" ? "text-amber-200" : "text-muted-2",
                )}
              >
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
              {(cat.note || cta) && (
                <p className="mt-1.5 flex flex-wrap gap-x-2 gap-y-1 text-[11.5px] leading-snug text-muted">
                  {cat.note && <span>{cat.note}</span>}
                  {cta &&
                    (cta.filed_item ? (
                      <FiledLink id={cta.filed_item} href={findingHref("item", cta.filed_item)} />
                    ) : (
                      <button
                        type="button"
                        onClick={() => file([ctaBody(health, key, cat, cta)])}
                        disabled={filing.isPending}
                        data-testid={`stack-health-cta-${key}`}
                        title="File this as work for an agent with the repo — the server never runs scanners"
                        className="text-accent hover:underline disabled:opacity-50"
                      >
                        {cta.kind === "rerun" ? "Re-run at head →" : "Add a scanner →"}
                      </button>
                    ))}
                </p>
              )}
              {cat.findings.length > 0 && (
                <ul className="mt-2 space-y-1.5">
                  {cat.findings.map((f, i) => {
                    const sev = sevMeta(f.severity);
                    const href = f.link_ref ? findingHref(f.link_kind, f.link_ref) : null;
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
                          <div className="flex flex-wrap items-center gap-x-1 font-mono text-[10px] text-muted-2">
                            <span>{f.path}</span>
                            {f.link_ref &&
                              (href ? (
                                <>
                                  {"·"}
                                  <Link
                                    to={href}
                                    className="text-accent hover:underline"
                                    data-testid="stack-health-finding-link"
                                    data-link-kind={f.link_kind || ""}
                                  >
                                    {f.link_ref} ↗
                                  </Link>
                                </>
                              ) : (
                                <span>· {f.link_ref}</span>
                              ))}
                            <span className="flex-1" />
                            {f.filed_key &&
                              (f.filed_item ? (
                                <FiledLink id={f.filed_item} href={findingHref("item", f.filed_item)} />
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => file([defragBody(health, key, cat, f)])}
                                  disabled={filing.isPending}
                                  title="File cleanup work from this finding"
                                  data-testid="stack-health-defrag"
                                  className="h-[22px] rounded-md border border-host px-2 font-sans text-small font-medium text-fg hover:border-accent hover:text-accent disabled:opacity-50"
                                >
                                  Defrag
                                </button>
                              ))}
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
