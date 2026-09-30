import { Check } from "lucide-react";

import type { RoadmapPhase } from "@/lib/types";

export function RoadmapBoard({ phases }: { phases: RoadmapPhase[] }) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
      {phases.map((p) => {
        const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
        return (
          <div key={p.key} className="flex flex-col rounded-[14px] border border-line-2 bg-surface/40 p-4">
            <div className="mb-1 flex items-center gap-2">
              <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: p.color }} />
              <span className="text-lead font-semibold">{p.name}</span>
              <span className="ml-auto font-mono text-[10px] text-faint">{p.window}</span>
            </div>
            <div className="mb-3 font-mono text-[10px] text-muted">
              {p.done} / {p.total} shipped
            </div>
            <div className="mb-4 h-1.5 overflow-hidden rounded-full bg-surface-4">
              <div className="h-full rounded-full" style={{ width: `${pct}%`, background: p.color }} />
            </div>
            <div className="space-y-2">
              {p.milestones.map((m, i) => (
                <div key={i} className="flex items-start gap-2.5">
                  <span
                    className="mt-0.5 flex h-4 w-4 flex-none items-center justify-center rounded-[5px] border"
                    style={{
                      borderColor: m.done ? p.color : "#2a333a",
                      background: m.done ? p.color : "transparent",
                    }}
                  >
                    {m.done && <Check size={11} className="text-bg" />}
                  </span>
                  <div className="min-w-0">
                    <div className="text-body leading-snug text-fg-2">{m.title}</div>
                    <div className="font-mono text-meta uppercase tracking-wide text-faint">{m.tag}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
