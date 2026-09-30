import { cn } from "@/lib/cn";
import type { LiveAgent } from "@/lib/types";

import { ROLE_TONE, agentActivity, ageLabel } from "./liveUtils";

export function PresenceStrip({
  agents,
  servedAt,
  focusId,
  onFocus,
}: {
  agents: { agent: LiveAgent; userLabel: string }[];
  servedAt: string;
  focusId: string | null;
  onFocus: (id: string | null) => void;
}) {
  if (agents.length === 0) return null;
  return (
    <div
      className="flex flex-none gap-1.5 overflow-x-auto border-b border-line px-5 py-2.5"
      role="list"
      aria-label="Agent presence"
    >
      <button
        type="button"
        aria-pressed={focusId === null}
        onClick={() => onFocus(null)}
        className={cn(
          "shrink-0 rounded-lg border px-2.5 py-1 text-[11px] transition-colors",
          focusId === null
            ? "border-control-hover bg-surface-3 text-fg"
            : "border-control bg-surface-2 text-muted hover:border-control-hover hover:text-fg-2",
        )}
      >
        All agents
      </button>
      {agents.map(({ agent: a }) => {
        const active = focusId === a.id;
        const offline = a.state === "offline";
        return (
          <button
            key={a.id}
            type="button"
            role="listitem"
            aria-pressed={active}
            onClick={() => onFocus(active ? null : a.id)}
            className={cn(
              "min-w-[9rem] shrink-0 rounded-lg border px-2.5 py-1.5 text-left transition-colors",
              active ? "border-control-hover bg-surface-3" : "border-control bg-surface-2 hover:border-control-hover",
              offline && "opacity-55",
              ROLE_TONE[a.role ?? "all-in-one"] ?? "text-muted border-control",
            )}
          >
            <div className="truncate text-[12px] text-fg-2">{a.label || a.id}</div>
            <div className="mt-0.5 truncate text-[10.5px] text-muted">{agentActivity(a, servedAt)}</div>
            <div className="mt-0.5 font-mono text-[9.5px] text-faint">
              {a.role || "all-in-one"} · {ageLabel(a.last_seen_at, servedAt)}
            </div>
          </button>
        );
      })}
    </div>
  );
}
