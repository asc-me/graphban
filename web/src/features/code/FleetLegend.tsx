import * as React from "react";

import { Avatar } from "@/components/ui/avatar";
import { holdersOf } from "@/lib/graph/presence";
import { cn } from "@/lib/cn";
import type { FleetPresence } from "@/lib/types";

/**
 * Who is in the codebase right now (PRD-20 D7).
 *
 * One chip per HUMAN, not per agent: the question this answers is "what is my team doing to
 * the codebase", and someone running three windows is still one teammate. Clicking a chip
 * solos them — their clouds stay, everyone else's fade — which is the screen we do not
 * currently have anywhere.
 *
 * The strip also carries the off-map count from D4. That number belongs next to the fleet it
 * describes rather than buried in an inspector: it is the difference between "nobody is
 * working here" and "we could not place what they are working on", and only one of those is
 * good news.
 */
export function FleetLegend({
  presence,
  soloUser,
  onSolo,
}: {
  presence: FleetPresence | undefined;
  soloUser: string | null;
  onSolo: (userId: string | null) => void;
}) {
  const [collapsed, setCollapsed] = React.useState(false);
  const [trayOpen, setTrayOpen] = React.useState(false);
  const holders = React.useMemo(() => holdersOf(presence), [presence]);
  const offMap = presence?.off_map ?? [];

  // Nothing held and nothing unplaceable: the fleet is genuinely idle, so say nothing rather
  // than render an empty chrome that looks like a broken feature.
  if (holders.length === 0 && offMap.length === 0) return null;

  return (
    <div className="pointer-events-auto absolute inset-x-3 bottom-3 z-10">
      {trayOpen && offMap.length > 0 && (
        <div className="mb-2 max-h-[168px] overflow-y-auto rounded-[11px] border border-line-hover bg-surface-3/95 p-2.5 shadow-[0_16px_40px_rgba(0,0,0,0.5)]">
          <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">
            Held, but not on this map
          </div>
          <div className="space-y-1">
            {offMap.map((row, i) => (
              <div key={`${row.agent_id}-${row.area}-${i}`} className="flex items-center gap-2 text-[12px]">
                <span
                  className="h-1.5 w-1.5 flex-none rounded-full"
                  style={{ background: row.user_color ?? "#8b949e" }}
                />
                {/* The RAW area text, deliberately. `vercel env` and `AGENTS.md` are different
                    kinds of thing and the server cannot tell them apart from the string, so
                    showing it verbatim lets a human do what the server cannot. */}
                <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-fg-2">
                  {row.area}
                </span>
                <span className="flex-none font-mono text-[9px] uppercase tracking-wide text-faint">
                  {row.reason}
                </span>
                <span className="flex-none font-mono text-[10px] text-muted">
                  {row.user_initials || row.agent_label || "—"}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5 rounded-[11px] border border-line-2 bg-surface-2/90 px-2 py-1.5 backdrop-blur">
        <button
          onClick={() => setCollapsed((c) => !c)}
          className="rounded px-1 font-mono text-[10px] uppercase tracking-wide text-faint hover:text-fg"
          aria-expanded={!collapsed}
        >
          {collapsed ? `fleet · ${holders.length}` : "fleet"}
        </button>

        {!collapsed &&
          holders.map((h) => {
            const active = soloUser === h.userId;
            return (
              <button
                key={h.userId}
                onClick={() => onSolo(active ? null : h.userId)}
                aria-pressed={active}
                title={`${h.agents.size} agent${h.agents.size === 1 ? "" : "s"}, ${h.nodes.size} node${h.nodes.size === 1 ? "" : "s"} held`}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-lg border px-1.5 py-1 text-[11.5px] transition-colors",
                  active
                    ? "border-line-hover bg-surface-4 text-fg"
                    : "border-transparent text-muted hover:border-line-2 hover:text-fg",
                )}
              >
                <Avatar initials={h.initials} color={h.color} size={18} />
                <span className="font-mono text-[10px]">
                  {h.agents.size}a · {h.nodes.size}n
                </span>
              </button>
            );
          })}

        {offMap.length > 0 && (
          <button
            onClick={() => setTrayOpen((t) => !t)}
            aria-expanded={trayOpen}
            title="Areas an agent holds that this graph cannot place"
            className={cn(
              "ml-auto rounded-lg border px-2 py-1 font-mono text-[10.5px] transition-colors",
              trayOpen
                ? "border-st-review/50 bg-st-review/10 text-st-review"
                : "border-line-2 text-muted hover:border-line-hover hover:text-fg",
            )}
          >
            {offMap.length} held area{offMap.length === 1 ? "" : "s"} not on this map
          </button>
        )}
      </div>
    </div>
  );
}
