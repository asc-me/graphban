import { AlertTriangle, Boxes, Search } from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { useGalaxy, useOrgs } from "@/lib/queries";
import { useGraphViewport } from "@/lib/graph/useGraphViewport";
import { useGraphLayout } from "@/lib/graph/useGraphLayout";
import type { LayoutEdge } from "@/lib/graph/layout";
import { projectPath } from "@/lib/routes";
import type { Galaxy, GalaxyEdge, GalaxyNode } from "@/lib/types";

const W = 1120;
const H = 640;

/**
 * The super galaxy (PRD-21 D3) — how this org's repos relate to each other.
 *
 * **Every edge names the file that proves it.** Hovering one shows the evidence, because
 * that is the entire difference between this graph and a guess: nothing here comes from
 * embedding similarity or shared vocabulary. Two repos that both describe "authentication"
 * are not related; two repos where one's lockfile names the other are.
 *
 * Not to be confused with `lib/graph/galaxy.ts`, which is PRD-20's *within-repo* component
 * collapse. Same word, unrelated feature — this one's nodes are whole projects.
 */
export function GalaxyView() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const { data, isLoading } = useGalaxy(org?.id);
  const [showStale, setShowStale] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [hovered, setHovered] = React.useState<GalaxyEdge | null>(null);

  const nodes = data?.nodes ?? [];
  // `allEdges` is what the org HAS; `edges` is what is currently drawn. The empty states
  // must key off the first: filtering stale edges out of the view does not mean the org
  // has no internal dependencies, and saying so would be a confident wrong answer
  // produced by the user's own toggle.
  const allEdges = data?.edges ?? [];
  const edges = React.useMemo(
    () => allEdges.filter((e) => showStale || e.fresh),
    [allEdges, showStale],
  );
  const staleCount = allEdges.filter((e) => !e.fresh).length;

  const ids = React.useMemo(() => nodes.map((n) => n.id), [nodes]);
  const layoutEdges: LayoutEdge[] = React.useMemo(
    () => edges.map((e) => ({ a: e.src, b: e.dst })),
    [edges],
  );
  const { pos } = useGraphLayout(ids, layoutEdges, { width: W, height: H });
  const view = useGraphViewport(W, H);

  const match = query.trim().toLowerCase();
  const dim = (n: GalaxyNode) =>
    !!match && !`${n.name} ${n.tag} ${n.provides.join(" ")}`.toLowerCase().includes(match);

  // `isLoading` is false while the org id is still resolving, so it cannot stand in for
  // "we have an answer". Keying the empty states on `data` instead stops the view
  // asserting "no projects" in the frame before it has looked — a confident claim made
  // from an absence, which is the exact failure this screen is built to name.
  const answered = data !== undefined;

  if (isLoading || !answered) {
    return <div className="px-6 py-10 font-mono text-[11px] text-faint-2">loading…</div>;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="max-w-[1300px] px-6 pt-6">
        <div className="flex flex-wrap items-center gap-2.5">
          <h1 className="text-[19px] font-semibold tracking-[-0.3px]">Galaxy</h1>
          <span className="rounded-full border border-purple/30 bg-purple/[0.07] px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.06em] text-purple">
            {nodes.length} repos · {edges.length} edges
          </span>
        </div>
        <p className="mt-1.5 max-w-[80ch] text-[12.5px] leading-relaxed text-muted">
          How this org's repositories depend on each other. Every edge comes from a manifest a
          deployment actually pushed — hover one to see the file that proves it. Nothing here is
          inferred from similarity.
        </p>

        <Collisions collisions={data?.collisions ?? []} nodes={nodes} />

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <div className="flex h-[30px] items-center gap-2 rounded-lg border border-line-2 bg-surface px-3">
            <Search size={12} className="shrink-0 text-faint-2" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="find a repo or package name…"
              aria-label="Find a repo"
              className="w-[220px] bg-transparent font-mono text-[11.5px] outline-none placeholder:text-faint-2"
            />
          </div>
          <button
            onClick={() => setShowStale((v) => !v)}
            aria-pressed={showStale}
            className={`h-[30px] rounded-lg border px-2.5 font-mono text-[9.5px] uppercase tracking-[0.05em] ${
              showStale
                ? "border-st-review/40 bg-st-review/[0.1] text-st-review"
                : "border-line-2 text-muted hover:text-fg"
            }`}
          >
            show stale{staleCount > 0 && ` · ${staleCount}`}
          </button>
          {staleCount > 0 && !showStale && (
            <span className="text-[11.5px] text-muted">
              {staleCount} edge{staleCount === 1 ? "" : "s"} hidden — the dependency is no longer
              declared, but the relationship is kept rather than deleted.
            </span>
          )}
        </div>
      </div>

      <div className="relative mx-6 my-4 min-h-[420px] flex-1 overflow-hidden rounded-[13px] border border-line bg-surface">
        {nodes.length === 0 ? (
          <EmptyGalaxy kind="no-projects" />
        ) : allEdges.length === 0 ? (
          <EmptyGalaxy kind={nodes.some((n) => n.pushed) ? "no-internal-deps" : "nothing-pushed"} />
        ) : edges.length === 0 ? (
          <EmptyGalaxy kind="all-stale" />
        ) : null}

        <svg
          ref={view.svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className={`h-full w-full touch-none ${view.panning ? "cursor-grabbing" : "cursor-grab"}`}
          {...view.svgHandlers}
        >
          <g transform={view.transform}>
            {edges.map((e) => {
              const a = pos[e.src];
              const b = pos[e.dst];
              if (!a || !b) return null;
              return (
                <g key={e.id}>
                  <line
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke={e.fresh ? "var(--color-line-3)" : "var(--color-st-review)"}
                    strokeOpacity={e.fresh ? 0.9 : 0.4}
                    strokeDasharray={e.fresh ? undefined : "4 4"}
                    strokeWidth={Math.min(4, 1 + e.weight)}
                    pointerEvents="none"
                  />
                  {/* A transparent hit area. The drawn line is 1–4px, and the evidence
                      popover is the whole point of this screen — leaving it behind a
                      hairline target makes the load-bearing interaction the hardest one
                      to perform. */}
                  <line
                    data-testid={`edge-${e.id}`}
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke="transparent"
                    strokeWidth={14}
                    onPointerEnter={() => setHovered(e)}
                    onPointerLeave={() => setHovered((h) => (h === e ? null : h))}
                    className="cursor-pointer"
                  />
                </g>
              );
            })}

            {nodes.map((n) => {
              const p = pos[n.id];
              if (!p) return null;
              // Size by code-graph node count — a real row count, so a repo that has
              // pushed nothing is visibly small rather than absent.
              const r = 12 + Math.min(26, Math.sqrt(n.node_count) * 2.2);
              return (
                <g key={n.id} opacity={dim(n) ? 0.25 : 1}>
                  <circle
                    cx={p.x}
                    cy={p.y}
                    r={r}
                    fill={n.pushed ? `${n.accent}22` : "transparent"}
                    stroke={n.accent}
                    strokeWidth={1.5}
                    strokeDasharray={n.pushed ? undefined : "3 3"}
                  />
                  <text
                    x={p.x}
                    y={p.y + r + 13}
                    textAnchor="middle"
                    className="fill-fg-2 font-mono"
                    style={{ fontSize: 11 }}
                  >
                    {n.tag}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>

        {hovered && <Evidence edge={hovered} nodes={nodes} />}
      </div>
    </div>
  );
}

/**
 * The evidence popover — the load-bearing interaction on this screen.
 *
 * Every edge in this product can name the file that proves it, and making that visible is
 * what separates the graph from a guess. A stale edge keeps its evidence for the same
 * reason: a relationship with no explanation is worse than a deleted one.
 */
function Evidence({ edge, nodes }: { edge: GalaxyEdge; nodes: GalaxyNode[] }) {
  const name = (id: string) => nodes.find((n) => n.id === id)?.tag ?? id;
  return (
    <div className="pointer-events-none absolute bottom-3 left-3 max-w-[420px] rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5 shadow-xl">
      <div className="font-mono text-[10px] uppercase tracking-[0.06em] text-faint-2">
        {name(edge.src)} → {name(edge.dst)} · {edge.kind.replace("_", " ")}
        {!edge.fresh && <span className="ml-2 text-st-review">stale</span>}
      </div>
      <div className="mt-1.5 flex flex-col gap-1">
        {edge.evidence.map((ev, i) => (
          <div key={i} className="font-mono text-[11px] text-fg-2">
            {ev.file} <span className="text-muted">→ {ev.fact}</span>
          </div>
        ))}
      </div>
      {!edge.fresh && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-st-review/80">
          No longer declared on the last push. Kept, not deleted — a dependency that quietly
          disappeared is information.
        </p>
      )}
      {edge.reason && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-muted">{edge.reason}</p>
      )}
    </div>
  );
}

/**
 * Three empty states, and they are three different facts.
 *
 * "No projects" is an empty org. "Nothing pushed" is an org whose *edges* are empty while
 * its repos are not — the nodes are still drawn. "No internal dependencies" is a
 * legitimate answer that must not look like a failure.
 */
function EmptyGalaxy({
  kind,
}: {
  kind: "no-projects" | "nothing-pushed" | "no-internal-deps" | "all-stale";
}) {
  const copy = {
    "no-projects": {
      title: "No projects yet",
      body: "This org has no repositories, so there is nothing to relate. Create a project and link a deployment to it.",
      tone: "text-muted",
    },
    "nothing-pushed": {
      title: "No deployment has pushed a manifest yet",
      body: "The repos are drawn — the org is not empty, the edges are. A dependency appears when a linked deployment pushes the manifest it parsed.",
      tone: "text-st-review",
    },
    "no-internal-deps": {
      title: "Every dependency resolved to an external package",
      body: "Manifests were pushed and read; none of them named a sibling repo in this org. That is a real answer about this org's shape, not a failure to compute one.",
      tone: "text-st-done",
    },
    "all-stale": {
      title: "Every dependency here has gone stale",
      body: "These relationships existed and are no longer declared on the latest push. They are kept, not deleted — turn on SHOW STALE to see them and what proved them.",
      tone: "text-st-review",
    },
  }[kind];

  return (
    <div className="pointer-events-none absolute inset-x-0 top-8 z-10 flex justify-center px-6">
      <div className="max-w-[460px] rounded-[13px] border border-line-2 bg-surface-2/95 px-4 py-3 text-center">
        <div className={`text-[13.5px] font-semibold ${copy.tone}`}>{copy.title}</div>
        <p className="mt-1.5 text-[12px] leading-relaxed text-muted">{copy.body}</p>
      </div>
    </div>
  );
}

/** A name two projects both claim draws no edge — so the screen says so out loud. */
function Collisions({
  collisions,
  nodes,
}: {
  collisions: Galaxy["collisions"];
  nodes: GalaxyNode[];
}) {
  if (collisions.length === 0) return null;
  const tagOf = (id: string) => nodes.find((n) => n.id === id)?.tag ?? id;
  return (
    <div className="mt-3 flex gap-2.5 rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2.5">
      <AlertTriangle size={14} className="mt-0.5 shrink-0 text-st-review" />
      <div className="min-w-0 flex-1 text-[11.5px] leading-relaxed text-st-review/85">
        {collisions.map((c) => (
          <div key={c.name}>
            <span className="font-mono text-fg-2">{c.name}</span> is claimed by{" "}
            {c.project_ids.map(tagOf).join(" and ")} — <strong className="font-semibold">no edge is drawn</strong>.
            An ambiguous name is a coin flip, and this graph does not guess.
          </div>
        ))}
      </div>
    </div>
  );
}

/** A node links into its project. Exported for the project-home "depends on" strip. */
export function GalaxyNodeLink({ node }: { node: GalaxyNode }) {
  return (
    <Link
      to={projectPath(node.tag)}
      className="inline-flex items-center gap-1.5 rounded-[5px] border border-line px-1.5 py-px hover:border-line-hover"
    >
      <Boxes size={11} style={{ color: node.accent }} />
      <span className="font-mono text-[10.5px] text-fg-2">{node.tag}</span>
    </Link>
  );
}
