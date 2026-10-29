import * as React from "react";

import { cn } from "@/lib/cn";
import { TYPE_META } from "@/lib/meta";
import { usePrdRequestLink, usePrdRequestSuggestions, usePrdRequests, useRequests } from "@/lib/queries";
import type { PrdSummary, RequestType } from "@/lib/types";

const EMPTY_COPY =
  "No bugs or feature requests are linked to this PRD yet. Link them from Requests, or accept a suggestion below.";

const STATUS_COLOR: Record<string, string> = {
  new: "var(--color-muted)",
  triaging: "var(--color-st-review)",
  linked: "var(--color-st-next)",
};

/** The list row's pill: a dot per request type with its count, or "no requests". */
export function RequestDots({ prd, onOpen }: { prd: PrdSummary; onOpen: () => void }) {
  const count = prd.request_count ?? 0;
  const types = Object.entries(prd.request_types ?? {}) as [RequestType, number][];
  return (
    <span
      role="button"
      tabIndex={0}
      data-testid="prd-request-dots"
      title={count ? `${count} linked requests · ${prd.request_votes ?? 0} votes — open` : "No linked requests"}
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          onOpen();
        }
      }}
      className={cn(
        "flex w-[112px] flex-none items-center justify-end gap-1.5 rounded-md border px-1.5 py-0.5 font-mono text-meta",
        count === 0
          ? "border-transparent text-faint"
          : "border-line-2 text-muted hover:border-control-hover",
      )}
    >
      {count === 0 ? (
        "no requests"
      ) : (
        types.map(([t, n]) => (
          <span key={t} className="flex items-center gap-1">
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: TYPE_META[t]?.color }} />
            {n}
          </span>
        ))
      )}
      {count > 0 && <span className="text-faint">req</span>}
    </span>
  );
}

function TypeBadge({ type }: { type: RequestType }) {
  const m = TYPE_META[type];
  return (
    <span
      className="rounded-[5px] border px-1.5 py-px font-mono text-[9px] uppercase tracking-wide"
      style={{ color: m.color, borderColor: m.border, background: m.bg }}
    >
      {m.label}
    </span>
  );
}

export function PrdRequestsPanel({ prdId, projectId }: { prdId: string; projectId: string }) {
  const { data: linked = [], isLoading, isError } = usePrdRequests(prdId);
  const { data: suggestions } = usePrdRequestSuggestions(prdId);
  // by / ago / the linked tracker item live on the request, not on the PRD link route.
  const { data: all = [] } = useRequests(projectId);
  const link = usePrdRequestLink(prdId);
  const [filter, setFilter] = React.useState<"all" | RequestType>("all");

  const byId = new Map(all.map((r) => [r.id, r]));
  const votes = linked.reduce((n, r) => n + r.votes, 0);
  const shown = linked.filter((r) => filter === "all" || r.type === filter).sort((a, b) => b.votes - a.votes);
  const typeCount = (t: RequestType) => linked.filter((r) => r.type === t).length;

  if (isLoading) return <p className="text-[13px] text-muted">Loading requests…</p>;
  if (isError) return <p className="text-[13px] text-st-blocked">Linked requests unavailable</p>;

  return (
    <div className="flex flex-col gap-3.5" data-testid="prd-requests-panel">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="font-mono text-[10px] tracking-wide text-faint">LINKED REQUESTS</span>
        <span className="font-mono text-[10px] text-muted">
          {linked.length} · {votes} votes
        </span>
        <div className="ml-auto flex flex-wrap gap-1">
          {(["all", ...(Object.keys(TYPE_META) as RequestType[])] as const).map((t) => (
            <button
              key={t}
              aria-pressed={filter === t}
              onClick={() => setFilter(t)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-[7px] border px-2 py-0.5 text-small transition-colors",
                filter === t ? "border-control-hover bg-surface-4 text-fg" : "border-control text-muted hover:text-fg-2",
              )}
            >
              {t !== "all" && (
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: TYPE_META[t].color }} />
              )}
              {t === "all" ? "All" : TYPE_META[t].label.charAt(0) + TYPE_META[t].label.slice(1).toLowerCase()}
              <span className="font-mono text-meta text-faint">{t === "all" ? linked.length : typeCount(t)}</span>
            </button>
          ))}
        </div>
      </div>

      {shown.map((r) => {
        const full = byId.get(r.id);
        return (
          <div key={r.id} className="flex flex-col gap-2 rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5">
            <div className="flex items-center gap-2">
              <span className="flex w-7 flex-none flex-col items-center font-mono text-[10.5px] leading-tight text-fg-2">
                <span className="text-[9px] text-faint">▲</span>
                {r.votes}
              </span>
              <TypeBadge type={r.type} />
              <span className="font-mono text-[10.5px] text-faint">{r.id}</span>
              <span className="min-w-0 flex-1 truncate text-[13px] text-fg-2">{r.title}</span>
              <span
                className="flex items-center gap-1.5 font-mono text-meta uppercase tracking-wide"
                style={{ color: STATUS_COLOR[r.status] ?? "var(--color-muted)" }}
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: STATUS_COLOR[r.status] ?? "var(--color-muted)" }} />
                {r.status}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2 pl-9">
              {full && (
                <span className="text-[11.5px] text-muted">
                  {full.by} · {full.ago} ago
                </span>
              )}
              {full?.linked_to && (
                <span className="rounded-[5px] border border-st-next/30 px-1.5 py-px font-mono text-meta text-st-next">
                  → {full.linked_to}
                </span>
              )}
              <button
                onClick={() => link.mutate({ requestId: r.id, add: false })}
                disabled={link.isPending}
                className="ml-auto h-6 rounded-md px-2 text-small text-faint hover:bg-surface-4 hover:text-ty-bug"
              >
                Unlink
              </button>
            </div>
          </div>
        );
      })}

      {shown.length === 0 && (
        <div className="rounded-[11px] border border-dashed border-line-2 px-3.5 py-5 text-center text-body leading-relaxed text-muted">
          {linked.length ? "No linked requests of this type." : EMPTY_COPY}
        </div>
      )}

      {suggestions && !suggestions.available && (
        <p className="font-mono text-[10.5px] text-faint">Suggestions unavailable</p>
      )}
      {suggestions?.available && suggestions.suggestions.length > 0 && (
        <div className="mt-1 flex flex-col gap-1.5">
          <span className="font-mono text-[10px] tracking-wide text-faint">SUGGESTED · DUPLICATE DETECTION</span>
          {suggestions.suggestions.map((s) => (
            <div key={s.request_id} className="flex items-center gap-2 rounded-[11px] border border-dashed border-line-2 px-3 py-2">
              <TypeBadge type={s.type} />
              <span className="font-mono text-[10.5px] text-faint">{s.request_id}</span>
              <span className="min-w-0 flex-1 truncate text-body text-fg-2">{s.title}</span>
              <span title="Similarity to this PRD's goals" className="font-mono text-[10px] text-st-review">
                {Math.round(s.score * 100)}% match
              </span>
              <button
                onClick={() => link.mutate({ requestId: s.request_id, add: true })}
                disabled={link.isPending}
                className="h-6 rounded-md border border-accent/35 bg-accent/10 px-2 text-small text-accent hover:bg-accent/15"
              >
                Link
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
