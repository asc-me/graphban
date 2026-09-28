import type { LiveKpis } from "./liveUtils";

export function KpiRow({ kpis }: { kpis: LiveKpis }) {
  return (
    <div className="grid flex-none grid-cols-2 gap-3 border-b border-line px-5 py-3 sm:grid-cols-4">
      <Kpi label="Online" value={String(kpis.online)} />
      <Kpi
        label="Touches / min"
        value={kpis.touchesPerMinute == null ? "not measured" : String(kpis.touchesPerMinute)}
      />
      <Kpi label="Planner calls" value={String(kpis.plannerCalls)} />
      <Kpi label="Refused (10m)" value={String(kpis.refused)} hint="from last observed call per agent" />
    </div>
  );
}

function Kpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3 py-2">
      <div className="font-mono text-[9.5px] uppercase tracking-wide text-faint">{label}</div>
      <div className="mt-0.5 text-[18px] font-semibold tabular-nums text-fg">{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-faint">{hint}</div>}
    </div>
  );
}
