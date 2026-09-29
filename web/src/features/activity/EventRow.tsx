import { Cog, KeyRound, User as UserIcon } from "lucide-react";

import { cn } from "@/lib/cn";
import type { Event } from "@/lib/types";

import { relTime, summarizeMeta, VERDICT_TITLE, verdictOf, type Verdict } from "./model";

/** One row: time, actor avatar, action, target, summary, the `actor via agent` line,
 *  surface, and the verdict chip (PRD-47 S10). The row is a single button — the pivots
 *  live in the panel, so nothing nests inside it. */
export function EventRow({
  event: e,
  refusals,
  focused,
  onOpen,
  innerRef,
}: {
  event: Event;
  refusals: string[] | null;
  focused: boolean;
  onOpen: () => void;
  innerRef?: (el: HTMLButtonElement | null) => void;
}) {
  // The agent that performed it (API key or assistant); the human behind it, if known.
  const agent = e.agent || (e.actor_type === "apikey" ? e.actor_label : "");
  const principal = e.principal || (e.actor_type === "apikey" ? "" : e.actor_label) || e.actor_id;
  const primary = principal || agent || "(unnamed actor)";
  return (
    <button
      type="button"
      ref={innerRef}
      onClick={onOpen}
      data-event-id={e.id}
      aria-current={focused ? "true" : undefined}
      className={cn(
        "flex w-full items-center gap-3 rounded-[10px] border px-3.5 py-2.5 text-left transition-colors",
        focused ? "border-line-hover bg-surface-3" : "border-line-2 bg-surface-2 hover:border-line-hover",
      )}
    >
      <ActorGlyph actorType={e.actor_type} name={primary} agent={agent} />

      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[13px]">
          <span className="font-medium text-ink">{primary}</span>
          {principal && agent && (
            <span className="text-[11px] text-faint">
              via <span className="text-purple">{agent}</span>
            </span>
          )}
          <span className="font-mono text-[11.5px] text-accent">{e.action}</span>
          {e.target_id && <span className="font-mono text-[11px] text-muted">{e.target_id}</span>}
          {!e.target_id && e.target_type && (
            <span className="font-mono text-[11px] text-faint">{e.target_type}</span>
          )}
        </span>
        {summarizeMeta(e.meta) && (
          <span className="mt-0.5 block truncate font-mono text-[10.5px] text-faint">
            {summarizeMeta(e.meta)}
          </span>
        )}
      </span>

      <span className="flex-none rounded border border-line px-1.5 py-0.5 font-mono text-[9px] text-faint">
        {e.surface}
      </span>
      <VerdictChip verdict={verdictOf(e, refusals)} />
      <span className="w-14 flex-none text-right font-mono text-[10.5px] text-faint" title={e.ts ?? ""}>
        {relTime(e.ts) || "no ts"}
      </span>
    </button>
  );
}

export function VerdictChip({ verdict }: { verdict: Verdict }) {
  const tone =
    verdict === "rejected"
      ? "border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] text-st-blocked"
      : verdict === "ok"
        ? "border-[#1c2620] bg-[rgba(95,208,122,0.1)] text-st-done"
        : "border-line-2 bg-surface-3 text-faint";
  return (
    <span
      title={VERDICT_TITLE[verdict]}
      className={cn(
        "flex-none rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide",
        tone,
      )}
    >
      {verdict}
    </span>
  );
}

function ActorGlyph({
  actorType,
  name,
  agent,
}: {
  actorType: Event["actor_type"];
  name: string;
  agent: string;
}) {
  const tone =
    actorType === "apikey"
      ? "bg-[rgba(167,139,250,0.12)] text-purple"
      : actorType === "system"
        ? "bg-[rgba(224,179,74,0.1)] text-st-review"
        : "bg-[rgba(95,208,122,0.1)] text-st-done";
  const title =
    actorType === "system"
      ? `${name} — recorded by the system, not by a person or an agent key`
      : `${name}${agent && name !== agent ? ` via ${agent}` : ""}`;
  return (
    <span
      className={cn("flex h-7 w-7 flex-none items-center justify-center rounded-full", tone)}
      title={title}
    >
      {actorType === "apikey" ? (
        <KeyRound size={13} />
      ) : actorType === "system" ? (
        <Cog size={13} />
      ) : (
        <UserIcon size={13} />
      )}
    </span>
  );
}
