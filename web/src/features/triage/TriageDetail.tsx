import type { ReactNode } from "react";

import { CommentThread } from "@/features/requests/CommentThread";
import type { TriageFeedRow } from "@/lib/types";

import { formatAge, type TriageAction } from "./actions";

const STATUS_COLOR: Record<string, string> = {
  in_progress: "#7ca2ff", review: "#e0b34a", blocked: "#ff6b6b", next: "#c9b8ff",
  backlog: "#8b949e", done: "#6ee7a0",
};

function Label({ children }: { children: ReactNode }) {
  return (
    <div className="mt-5 font-mono text-micro uppercase tracking-[0.07em] text-faint-2">{children}</div>
  );
}

function Paths({ label, paths }: { label: string; paths: string[] }) {
  if (!paths.length) return null;
  return (
    <>
      <Label>{label}</Label>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {paths.map((p) => (
          <span key={p} className="rounded-[5px] border border-line bg-surface px-1.5 py-px font-mono text-meta text-muted">
            {p}
          </span>
        ))}
      </div>
    </>
  );
}

export function TriageDetail({
  row,
  actions,
  onAction,
}: {
  row: TriageFeedRow;
  actions: TriageAction[];
  onAction: (a: TriageAction) => void;
}) {
  const metaLine =
    row.kind === "blocker"
      ? `claimed by ${row.owner ?? "nobody"} · blocked ${formatAge(row.blocked_seconds)}`
      : row.kind === "collision"
        ? `detected ${formatAge(row.age_seconds)} ago`
        : `${row.by || "anonymous"} · ${row.source} · ↑ ${row.votes} · ${formatAge(row.age_seconds)}`;

  return (
    <div className="px-6 py-5">
      <div className="flex items-center gap-2 font-mono text-meta text-faint-2">
        <span className="uppercase text-fg-2">{row.kind}</span>
        <span>{row.id}</span>
        {row.kind === "collision" && row.predicted && (
          <span className="rounded border border-purple/30 px-1.5 py-px text-micro uppercase text-purple">predicted</span>
        )}
      </div>
      <div className="mt-1 font-mono text-meta text-muted">{metaLine}</div>
      <h2 className="mt-2 text-[18px] font-semibold leading-snug">{row.title}</h2>

      <div className="mt-4 flex flex-wrap gap-2" role="toolbar" aria-label="Actions">
        {actions.map((a) => (
          <button
            key={a.key}
            onClick={() => onAction(a)}
            className={`flex h-8 items-center gap-2 rounded-md border px-3 text-small ${
              a.primary ? "border-accent bg-accent text-bg" : "border-control text-fg-2 hover:bg-surface-3"
            }`}
          >
            <kbd className={`rounded px-1 font-mono text-micro ${a.primary ? "bg-black/15" : "bg-surface-3 text-muted"}`}>
              {a.key.toUpperCase()}
            </kbd>
            {a.label}
            {a.primary && <span className="font-mono text-micro opacity-60">↵</span>}
          </button>
        ))}
      </div>

      {row.kind === "blocker" && (
        <>
          <Label>{row.decision ? "AGENT ASKED" : "WHY IT'S BLOCKED"}</Label>
          <p className="mt-1.5 text-body leading-relaxed text-fg-2">
            {row.decision?.question ?? (row.reason || "No reason was recorded.")}
          </p>
          {row.waiting_on && (
            <>
              <Label>WAITING ON</Label>
              <div className="mt-1.5 flex items-center gap-2 rounded-md border border-line bg-surface px-3 py-2">
                <span className="font-mono text-meta text-fg-2">{row.waiting_on.id}</span>
                <span className="min-w-0 flex-1 truncate text-small text-muted">{row.waiting_on.title}</span>
                <span className="font-mono text-micro uppercase" style={{ color: STATUS_COLOR[row.waiting_on.status] ?? "#8b949e" }}>
                  {row.waiting_on.status.replace("_", " ")}
                </span>
              </div>
            </>
          )}
          {row.decision && (
            <>
              <Label>AGENT'S OPTIONS</Label>
              <ul className="mt-1.5 space-y-1">
                {row.decision.options.map((o) => (
                  <li key={o} className="text-small text-fg-2">{o}</li>
                ))}
              </ul>
            </>
          )}
          <Paths label="CODE TOUCH-AREA" paths={row.paths} />
        </>
      )}

      {row.kind === "collision" && (
        <>
          <Label>MEMBERS, IN ORDER</Label>
          <ol className="mt-1.5 space-y-1.5">
            {row.members.map((m, i) => (
              <li key={m.id} className="flex items-center gap-2 rounded-md border border-line bg-surface px-3 py-2">
                <span className="font-mono text-meta text-faint-2">{i + 1}</span>
                <span className="font-mono text-meta text-fg-2">{m.id}</span>
                <span className="min-w-0 flex-1 truncate text-small text-muted">{m.title}</span>
                <span className="font-mono text-micro text-faint-2">{m.owner ?? "unclaimed"}</span>
                <span className="font-mono text-micro uppercase" style={{ color: STATUS_COLOR[m.status] ?? "#8b949e" }}>
                  {m.status.replace("_", " ")}
                </span>
              </li>
            ))}
          </ol>
          <Label>RECOMMENDATION · {row.risk.toUpperCase()}</Label>
          <p className="mt-1.5 text-body leading-relaxed text-fg-2">{row.recommendation}</p>
          <Paths label="SHARED PATHS" paths={row.paths} />
        </>
      )}

      {row.kind !== "blocker" && row.kind !== "collision" && (
        <>
          {row.body && <p className="mt-5 text-body leading-relaxed text-fg-2">{row.body}</p>}
          {row.duplicate && (
            <>
              <Label>LIKELY DUPLICATE</Label>
              <div className="mt-1.5 flex items-center gap-2 rounded-md border border-st-next/30 bg-st-next/[0.05] px-3 py-2">
                <span className="font-mono text-meta text-st-next">{row.duplicate.id}</span>
                <span className="min-w-0 flex-1 truncate text-small text-muted">{row.duplicate.title}</span>
                <span className="font-mono text-micro text-faint-2">{row.duplicate.status}</span>
                <span className="font-mono text-meta text-st-next">{Math.round(row.duplicate.score * 100)}% similar</span>
              </div>
            </>
          )}
          {row.suggested_prd && (
            <p className="mt-4 font-mono text-meta text-muted">
              Fits <span className="text-fg-2">{row.suggested_prd.id}</span>{" "}
              {Math.round(row.suggested_prd.score * 100)}% — will link on accept
            </p>
          )}
          <Paths label="CODE TOUCH-AREA" paths={row.paths} />
          {row.kind !== "feedback" && (
            <div className="mt-5">
              <CommentThread requestId={row.id} triageKey />
            </div>
          )}
        </>
      )}
    </div>
  );
}
