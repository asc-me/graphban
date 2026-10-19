import { Boxes, Printer } from "lucide-react";
import * as React from "react";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import {
  FETCH_FAILED,
  PlannerError,
  CardGridSkeleton,
} from "@/components/planner/PlannerStates";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useStack } from "@/lib/queries";
import type { StackEdge, StackLiveness, StackNode, StackTopology } from "@/lib/types";

const LIVENESS_META: Record<
  StackLiveness,
  { label: string; className: string; dot: string }
> = {
  reachable: {
    label: "reachable",
    className: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
    dot: "bg-emerald-400",
  },
  unreachable: {
    label: "unreachable",
    className: "border-rose-500/40 bg-rose-500/10 text-rose-200",
    dot: "bg-rose-400",
  },
  unknown: {
    label: "unknown",
    className: "border-amber-500/40 bg-amber-500/10 text-amber-100",
    dot: "bg-amber-400",
  },
  not_configured: {
    label: "not configured",
    className: "border-line bg-surface-2 text-muted",
    dot: "bg-muted-2",
  },
  uncalibrated: {
    label: "uncalibrated",
    className: "border-violet-500/40 bg-violet-500/10 text-violet-200",
    dot: "bg-violet-400",
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
 * (GRPH-1034 / PRD-P51 §2). Layout-first host groups — distinct from the
 * force-directed Code graph. Every box/edge comes from `GET /api/platform/stack`.
 */
export function StackView() {
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const q = useStack(activeId);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);

  const data = q.data;
  const selected = data?.nodes.find((n) => n.id === selectedId) ?? null;

  React.useEffect(() => {
    if (!data) return;
    if (selectedId && !data.nodes.some((n) => n.id === selectedId)) {
      setSelectedId(null);
    }
  }, [data, selectedId]);

  const onPrint = () => {
    window.print();
  };

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="stack-view">
      <PlaceHeader
        viewName={`Stack ${scope}`}
        purpose="Derived architecture of what runs, where, and what each piece depends on — deployables and externals, not modules."
        action={
          <button
            type="button"
            onClick={onPrint}
            className="inline-flex items-center gap-1.5 rounded-[8px] border border-control bg-surface-2 px-2.5 py-1.5 text-[12px] text-fg-2 transition-colors hover:border-control-hover print:hidden"
          >
            <Printer size={14} />
            Print / PDF
          </button>
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
            <div className="min-h-0 flex-1 overflow-auto p-5 print:p-2">
              <IdentityStrip data={data} />
              <EmptiesBanner empties={data.empties} catalog={data.catalog} />
              <HostCanvas
                data={data}
                selectedId={selectedId}
                onSelect={setSelectedId}
              />
              <Legend />
            </div>
            {selected && (
              <Inspector
                node={selected}
                edges={data.edges}
                onClose={() => setSelectedId(null)}
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
      className="mb-4 flex flex-wrap gap-x-4 gap-y-1 rounded-[10px] border border-line bg-surface-2/60 px-3 py-2 font-mono text-small text-muted"
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
      className="mb-4 flex flex-wrap gap-2"
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

function HostCanvas({
  data,
  selectedId,
  onSelect,
}: {
  data: StackTopology;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const byId = React.useMemo(
    () => Object.fromEntries(data.nodes.map((n) => [n.id, n])),
    [data.nodes],
  );

  return (
    <div
      className="grid gap-4 lg:grid-cols-2 xl:grid-cols-2 print:grid-cols-2"
      data-testid="stack-canvas"
    >
      {data.hosts.map((host) => {
        const nodes = host.nodes
          .map((id) => byId[id])
          .filter((n): n is StackNode => !!n);
        if (!nodes.length && host.id !== "external") {
          // Still show agent/user hosts when empty so absence is visible.
        }
        return (
          <HostGroup
            key={host.id}
            label={host.label}
            hostId={host.id}
            nodes={nodes}
            edges={data.edges}
            selectedId={selectedId}
            onSelect={onSelect}
          />
        );
      })}
    </div>
  );
}

function HostGroup({
  label,
  hostId,
  nodes,
  edges,
  selectedId,
  onSelect,
}: {
  label: string;
  hostId: string;
  nodes: StackNode[];
  edges: StackEdge[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <section
      className="rounded-[12px] border border-line bg-[rgba(9,11,13,0.45)] p-3"
      data-testid={`stack-host-${hostId}`}
    >
      <header className="mb-3 flex items-center gap-2">
        <Boxes size={14} className="text-muted" />
        <h2 className="text-[12px] font-medium uppercase tracking-[0.06em] text-muted">
          {label}
        </h2>
        <span className="font-mono text-[10px] text-muted-2">{nodes.length}</span>
      </header>
      {nodes.length === 0 ? (
        <p className="px-1 py-3 text-[12px] text-muted">Nothing in this host group.</p>
      ) : (
        <div className="flex flex-wrap gap-2.5">
          {nodes.map((n) => (
            <NodeCard
              key={n.id}
              node={n}
              selected={selectedId === n.id}
              edgeCount={edges.filter((e) => e.from === n.id || e.to === n.id).length}
              onSelect={() => onSelect(n.id)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function NodeCard({
  node,
  selected,
  edgeCount,
  onSelect,
}: {
  node: StackNode;
  selected: boolean;
  edgeCount: number;
  onSelect: () => void;
}) {
  const live = liveMeta(node.liveness);
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid={`stack-node-${node.id}`}
      aria-pressed={selected}
      className={cn(
        "min-w-[148px] max-w-[220px] flex-1 rounded-[10px] border px-3 py-2.5 text-left transition-colors",
        "bg-surface-2 hover:border-control-hover",
        selected ? "border-accent ring-1 ring-accent/40" : "border-control",
      )}
    >
      <div className="mb-1.5 flex items-start justify-between gap-2">
        <div>
          <div className="text-[13px] font-medium text-fg">{node.label}</div>
          <div className="text-[10px] text-muted">{RING_LABEL[node.ring] || node.kind}</div>
        </div>
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]",
            live.className,
          )}
          title={live.label}
        >
          <span className={cn("h-1.5 w-1.5 rounded-full", live.dot)} />
          {live.label}
        </span>
      </div>
      <div className="flex flex-wrap gap-1">
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
        {node.surfaces.length > 3 && (
          <span className="text-[10px] text-muted-2">+{node.surfaces.length - 3}</span>
        )}
      </div>
      <div className="mt-2 font-mono text-[10px] text-muted-2">
        {edgeCount} connection{edgeCount === 1 ? "" : "s"}
      </div>
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

function Inspector({
  node,
  edges,
  onClose,
}: {
  node: StackNode;
  edges: StackEdge[];
  onClose: () => void;
}) {
  const live = liveMeta(node.liveness);
  const connections = edges.filter((e) => e.from === node.id || e.to === node.id);

  return (
    <aside
      className="flex w-[300px] flex-none flex-col border-l border-line bg-[rgba(9,11,13,0.7)] print:hidden"
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
              {connections.map((e, i) => (
                <li key={`${e.from}-${e.to}-${i}`} className="font-mono text-small text-fg-2">
                  <span className="text-muted">{e.from}</span>
                  <span className="mx-1 text-accent">→</span>
                  <span className="text-muted">{e.to}</span>
                  <span className="ml-2 text-muted-2">{e.protocol || e.label}</span>
                </li>
              ))}
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

function Legend() {
  return (
    <div
      className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-3 text-small text-muted print:break-inside-avoid"
      data-testid="stack-legend"
    >
      <span className="uppercase tracking-[0.06em]">Liveness</span>
      {(Object.keys(LIVENESS_META) as StackLiveness[]).map((k) => {
        const m = LIVENESS_META[k];
        return (
          <span key={k} className="inline-flex items-center gap-1.5">
            <span className={cn("h-1.5 w-1.5 rounded-full", m.dot)} />
            {m.label}
          </span>
        );
      })}
      <span className="ml-2 uppercase tracking-[0.06em]">Scope</span>
      <span>instance-wide · project-scoped</span>
    </div>
  );
}
