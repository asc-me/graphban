import type { ReactNode } from "react";

import { CommentThread } from "@/features/requests/CommentThread";
import type { TriageFeedRow } from "@/lib/types";

import { formatAge, type TriageAction } from "./actions";

const STATUS_TEXT: Record<string, string> = {
  in_progress: "text-st-next", review: "text-st-review", blocked: "text-st-blocked", next: "text-purple",
  backlog: "text-muted", done: "text-st-done",
};
const statusText = (s: string) => STATUS_TEXT[s] ?? "text-muted";

const KIND_PILL: Record<string, string> = {
  blocker: "border-st-blocked/40 text-st-blocked",
  collision: "border-st-review/40 text-st-review",
  feature: "border-accent/40 text-accent",
};
const kindPill = (k: string) => KIND_PILL[k] ?? "border-control text-fg-2";

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
      <div className="flex flex-wrap items-center gap-2 font-mono text-meta text-faint-2">
        <span className={`rounded-full border px-2 py-px uppercase ${kindPill(row.kind)}`}>{row.kind}</span>
        <span>{row.id}</span>
        {row.kind === "collision" && row.predicted && (
          <span className="rounded border border-purple/30 px-1.5 py-px text-micro uppercase text-purple">predicted</span>
        )}
        <span className="flex-1" />
        <span className="text-muted">{metaLine}</span>
      </div>
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
          </button>
        ))}
      </div>
      <div data-testid="triage-rule" className="mt-4 border-t border-line" />

      {row.kind === "blocker" && (
        <>
          <div className="mt-5 rounded-lg border border-st-blocked/30 bg-st-blocked/[0.07] px-3 py-2.5">
            <div className="font-mono text-micro uppercase tracking-[0.07em] text-st-blocked">
              {row.decision ? "AGENT ASKED" : "WHY IT'S BLOCKED"}
            </div>
            <p className="mt-1.5 text-body leading-relaxed text-fg-2">
              {row.decision?.question ?? (row.reason || "No reason was recorded.")}
            </p>
            {row.decision && (
              <div className="mt-2 flex flex-wrap gap-1.5" aria-label="Agent's options">
                {row.decision.options.map((o) => (
                  <span key={o} className="rounded-full border border-st-blocked/30 px-2.5 py-0.5 text-small text-fg-2">{o}</span>
                ))}
              </div>
            )}
          </div>
          {row.waiting_on && (
            <>
              <Label>WAITING ON</Label>
              <div className="mt-1.5 flex items-center gap-2 rounded-md border border-line bg-surface px-3 py-2">
                <span className="font-mono text-meta text-fg-2">{row.waiting_on.id}</span>
                <span className="min-w-0 flex-1 truncate text-small text-muted">{row.waiting_on.title}</span>
                <span className={`font-mono text-micro uppercase ${statusText(row.waiting_on.status)}`}>
                  {row.waiting_on.status.replace("_", " ")}
                </span>
              </div>
            </>
          )}
          <Paths label="CODE TOUCH-AREA" paths={row.paths} />
        </>
      )}

      {row.kind === "collision" && (
        <>
          <Label>ITEMS IN THIS CLUSTER</Label>
          <ol className="mt-1.5 divide-y divide-line overflow-hidden rounded-md border border-line bg-surface">
            {row.members.map((m, i) => (
              <li key={m.id} className="flex items-center gap-2 px-3 py-2">
                <span className="font-mono text-meta text-faint-2">{i + 1}</span>
                <span className="font-mono text-meta text-fg-2">{m.id}</span>
                <span className="min-w-0 flex-1 truncate text-small text-muted">{m.title}</span>
                <span className="font-mono text-micro text-faint-2">{m.owner ?? "unclaimed"}</span>
                <span className={`font-mono text-micro uppercase ${statusText(m.status)}`}>
                  {m.status.replace("_", " ")}
                </span>
              </li>
            ))}
          </ol>
          <div className="mt-4 rounded-lg border border-st-review/30 bg-st-review/[0.07] px-3 py-2.5">
            <div className="font-mono text-micro uppercase tracking-[0.07em] text-st-review">
              RECOMMENDATION · {row.risk.toUpperCase()}
            </div>
            <p className="mt-1.5 text-body leading-relaxed text-fg-2">{row.recommendation}</p>
          </div>
          <Paths label="SHARED PATHS" paths={row.paths} />
        </>
      )}

      {row.kind !== "blocker" && row.kind !== "collision" && (
        <>
          {row.body && <p className="mt-5 text-body leading-relaxed text-fg-2">{row.body}</p>}
          {row.duplicate && (
            <>
              <div className="mt-4 rounded-lg border border-st-next/30 bg-st-next/[0.07] px-3 py-2.5">
                <div className="flex items-center gap-1.5 font-mono text-micro uppercase tracking-[0.07em] text-st-next">
                  <span aria-hidden>⧉</span>
                  LIKELY DUPLICATE · {Math.round(row.duplicate.score * 100)}%
                </div>
                <div className="mt-1.5 flex items-center gap-2">
                  <span className="font-mono text-meta text-st-next">{row.duplicate.id}</span>
                  <span className="min-w-0 flex-1 truncate text-small text-fg-2">{row.duplicate.title}</span>
                  <span className="font-mono text-micro text-faint-2">{row.duplicate.status}</span>
                </div>
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
