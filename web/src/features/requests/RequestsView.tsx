import { ChevronRight, ChevronUp, ExternalLink } from "lucide-react";
import * as React from "react";

import { LinkedCode } from "@/features/code/LinkedCode";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { TYPE_META } from "@/lib/meta";
import { useRequests, useVoteRequest } from "@/lib/queries";
import type { RequestItem, RequestType } from "@/lib/types";

import { LinkDialog } from "./LinkDialog";

const STATUS_COLOR: Record<string, string> = {
  new: "#8b949e",
  triaging: "#e0b34a",
  linked: "#7ca2ff",
};

type Filter = "all" | RequestType;

export function RequestsView() {
  const { activeId } = useProjectCtx();
  const { data: requests = [], isLoading } = useRequests(activeId);
  const vote = useVoteRequest();
  const [filter, setFilter] = React.useState<Filter>("all");

  const counts = React.useMemo(() => {
    const c: Record<string, number> = {};
    for (const r of requests) c[r.type] = (c[r.type] ?? 0) + 1;
    return c;
  }, [requests]);

  const visible = requests.filter((r) => filter === "all" || r.type === filter);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-line px-5 py-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Requests</h1>
        <p className="mt-0.5 text-[12.5px] text-muted">
          Triage queue from the public form. Auto-duplicate detection links submissions to existing items & memory.
        </p>
      </div>

      <div className="flex flex-none flex-wrap items-center gap-1.5 border-b border-line px-5 py-2.5">
        <Chip active={filter === "all"} onClick={() => setFilter("all")} label="All" count={requests.length} />
        {(Object.keys(TYPE_META) as RequestType[]).map((t) => (
          <Chip
            key={t}
            active={filter === t}
            onClick={() => setFilter(t)}
            label={TYPE_META[t].label}
            count={counts[t] ?? 0}
            color={TYPE_META[t].color}
          />
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {isLoading ? (
          <div className="p-8 text-center text-[13px] text-muted">Loading queue…</div>
        ) : (
          <div className="space-y-2">
            {visible.map((r) => (
              <RequestRow
                key={r.id}
                request={r}
                activeId={activeId}
                onVote={() => vote.mutate({ id: r.id, delta: 1 })}
              />
            ))}
            {visible.length === 0 && (
              <div className="p-8 text-center text-[13px] text-muted">No requests match.</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function RequestRow({
  request,
  activeId,
  onVote,
}: {
  request: RequestItem;
  activeId?: string;
  onVote: () => void;
}) {
  const meta = TYPE_META[request.type];
  const [open, setOpen] = React.useState(false);
  return (
    <div className="rounded-[12px] border border-line-2 bg-surface-2 transition-colors hover:border-line-hover">
      <div className="flex items-center gap-3 px-3 py-2.5">
        <button
          onClick={onVote}
          className="flex flex-none flex-col items-center gap-0.5 rounded-lg border border-line-2 bg-surface px-2 py-1.5 transition-colors hover:border-accent/40"
          title="Upvote"
        >
          <ChevronUp size={13} className="text-accent" />
          <span className="font-mono text-[11px] text-fg-2">{request.votes}</span>
        </button>

        <span
          className="flex-none rounded-md border px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-wide"
          style={{ color: meta.color, background: meta.bg, borderColor: meta.border }}
        >
          {meta.label}
        </span>

        <button
          onClick={() => setOpen((o) => !o)}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          aria-expanded={open}
          title="Show detail & linked code"
        >
          <ChevronRight
            size={13}
            className={cn("flex-none text-faint transition-transform", open && "rotate-90")}
          />
          <span className="min-w-0 flex-1 truncate text-[13px] text-fg-2">{request.title}</span>
        </button>

        {request.attachment_ids?.length > 0 && <Attachments ids={request.attachment_ids} />}
        {request.source_url && <SourceLink url={request.source_url} />}

        <span className="flex-none font-mono text-[10.5px] text-faint">{request.by}</span>
        <span className="hidden flex-none font-mono text-[10px] text-faint-2 sm:inline">{request.ago}</span>

        <span
          className="flex flex-none items-center gap-1.5 font-mono text-[10px] uppercase tracking-wide"
          style={{ color: STATUS_COLOR[request.status] ?? "#8b949e" }}
        >
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: STATUS_COLOR[request.status] ?? "#8b949e" }}
          />
          {request.status}
        </span>

        <LinkDialog request={request} />
      </div>

      {open && (
        <div className="animate-fade space-y-3 border-t border-line px-3 py-3">
          {request.detail && (
            <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-fg-2">{request.detail}</p>
          )}
          <div>
            <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">Linked code</div>
            <LinkedCode refId={request.id} projectId={activeId} />
          </div>
        </div>
      )}
    </div>
  );
}

function Attachments({ ids }: { ids: string[] }) {
  return (
    <div className="hidden flex-none items-center gap-1 sm:flex">
      {ids.slice(0, 3).map((id) => (
        <a key={id} href={`/api/public/attachments/${id}`} target="_blank" rel="noreferrer noopener" title="Screenshot">
          <img
            src={`/api/public/attachments/${id}`}
            alt="attachment"
            className="h-6 w-6 rounded border border-line-2 object-cover transition-opacity hover:opacity-80"
          />
        </a>
      ))}
      {ids.length > 3 && <span className="font-mono text-[10px] text-faint">+{ids.length - 3}</span>}
    </div>
  );
}

function SourceLink({ url }: { url: string }) {
  let host = url;
  try {
    host = new URL(url).hostname.replace(/^www\./, "");
  } catch {
    /* keep raw */
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      title={`Submitted from ${url}`}
      className="hidden flex-none items-center gap-1 font-mono text-[10px] text-faint hover:text-accent md:inline-flex"
    >
      <ExternalLink size={11} />
      <span className="max-w-[120px] truncate">{host}</span>
    </a>
  );
}

function Chip({
  active,
  onClick,
  label,
  count,
  color,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  color?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] transition-colors",
        active
          ? "border-line-hover bg-surface-3 text-fg"
          : "border-line-2 bg-surface-2 text-muted hover:border-line-3 hover:text-fg-2",
      )}
    >
      {color && <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />}
      {label}
      <span className="font-mono text-[10px] text-faint">{count}</span>
    </button>
  );
}
