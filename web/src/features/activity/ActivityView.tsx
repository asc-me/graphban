import { KeyRound, User as UserIcon } from "lucide-react";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { useProjectCtx } from "@/features/ProjectContext";
import { useEvents } from "@/lib/queries";
import type { Event } from "@/lib/types";

/** The audit ledger (AL-43): who did what, most-recent-first. */
export function ActivityView() {
  const { activeId } = useProjectCtx();
  const { data, isLoading } = useEvents(activeId);

  if (isLoading || !data) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="Activity"
        purpose="Every accepted mutation, attributed to the agent key or user that made it."
        action={<div className="font-mono text-[10.5px] text-faint">{data.total} EVENTS</div>}
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {data.results.length === 0 ? (
          <div className="mt-16 text-center text-[13px] text-muted">
            No activity yet. Agent and user mutations will appear here.
          </div>
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-1.5">
            {data.results.map((e) => (
              <EventRow key={e.id} event={e} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function EventRow({ event: e }: { event: Event }) {
  // The agent that performed it (API key or assistant); the human behind it, if known.
  const agent = e.agent || (e.actor_type === "apikey" ? e.actor_label : "");
  const principal = e.principal || (e.actor_type === "apikey" ? "" : e.actor_label) || e.actor_id;
  const isAgent = !!agent;
  const primary = principal || agent;
  return (
    <div className="flex items-center gap-3 rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5">
      <span
        className={`flex h-7 w-7 flex-none items-center justify-center rounded-full ${
          isAgent ? "bg-[rgba(167,139,250,0.12)] text-[#a78bfa]" : "bg-[rgba(95,208,122,0.1)] text-st-done"
        }`}
        title={isAgent ? `${primary}${principal && agent ? ` via ${agent}` : ""}` : e.actor_type}
      >
        {isAgent ? <KeyRound size={13} /> : <UserIcon size={13} />}
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-[13px]">
          <span className="font-medium text-ink">{primary}</span>
          {principal && agent && (
            <span className="text-[11px] text-faint">
              via <span className="text-[#a78bfa]">{agent}</span>
            </span>
          )}
          <span className="font-mono text-[11.5px] text-accent">{e.action}</span>
          {e.target_id && <span className="font-mono text-[11px] text-muted">{e.target_id}</span>}
        </div>
        {e.meta && summarizeMeta(e.meta) && (
          <div className="mt-0.5 truncate font-mono text-[10.5px] text-faint">{summarizeMeta(e.meta)}</div>
        )}
      </div>

      <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide text-faint">
        {e.surface}
      </span>
      <span className="flex-none font-mono text-[10.5px] text-faint" title={e.ts ?? ""}>
        {relTime(e.ts)}
      </span>
    </div>
  );
}

const META_DETAIL_MAX = 120;

function truncateMeta(s: string): string {
  return s.length <= META_DETAIL_MAX ? s : `${s.slice(0, META_DETAIL_MAX - 1)}…`;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Evidence receipts and other nested meta values — never `String(object)`. */
function formatMetaValue(v: unknown): string {
  if (Array.isArray(v)) return v.map(formatMetaValue).join(", ");
  if (isRecord(v)) {
    const kind = typeof v.kind === "string" ? v.kind : "";
    const detail = typeof v.detail === "string" ? v.detail : "";
    if (kind || detail) {
      if (kind && detail) return `${kind} — ${truncateMeta(detail)}`;
      return truncateMeta(kind || detail);
    }
    return Object.entries(v)
      .map(([k, val]) => `${k}: ${formatMetaValue(val)}`)
      .join(", ");
  }
  return String(v);
}

function summarizeMeta(meta: Record<string, unknown>): string {
  return Object.entries(meta)
    .filter(([k]) => k !== "principal" && k !== "origin") // shown in the header (AL-197)
    .map(([k, v]) => `${k}: ${formatMetaValue(v)}`)
    .join(" · ");
}

function relTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
