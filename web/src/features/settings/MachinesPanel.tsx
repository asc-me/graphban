import { Button } from "@/components/ui/button";
import { errorDetail } from "@/lib/errors";
import { useMachines, useRemoveMachine } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import type { Machine } from "@/lib/types";

/** A machine with no report for this long reads "last seen <date>" (GRPH-1442). */
export const STALE_DAYS = 14;

/**
 * ONE state per machine, worded and coloured the same here and on the dashboard card:
 * ✓ CLEAN (green), × REPORTED · n FAIL (failure red), ○ NEVER REPORTED (grey) — plus
 * ? REPORTED · UNKNOWN (muted) for a report with no FAIL that was still not all-PASS.
 */
export function MachineState({ machine }: { machine: Pick<Machine, "state" | "fail_count"> & { unknowns?: number } | null }) {
  if (!machine) {
    return <span data-state="never" className="font-mono text-[11px] text-faint">○ NEVER REPORTED</span>;
  }
  if (machine.state === "failed") {
    return (
      <span data-state="failed" className="font-mono text-[11px] text-danger">
        × REPORTED · {machine.fail_count} FAIL
      </span>
    );
  }
  if (machine.state === "unknown") {
    // No FAIL, but not all PASS either: a check that could not run is never a pass.
    return (
      <span data-state="unknown" className="font-mono text-[11px] text-muted">
        ? REPORTED · {machine.unknowns ? `${machine.unknowns} ` : ""}UNKNOWN
      </span>
    );
  }
  return <span data-state="clean" className="font-mono text-[11px] text-accent">✓ CLEAN</span>;
}

export function isStale(m: Machine, now = Date.now()): boolean {
  const t = Date.parse(m.last_reported_at);
  return Number.isFinite(t) && now - t > STALE_DAYS * 86400_000;
}

export function MachinesPanel() {
  const q = useMachines();
  const remove = useRemoveMachine();
  const rows = q.data ?? [];
  return (
    <div className="space-y-4 p-5" data-testid="machines-panel">
      <div>
        <h2 className="text-lead font-semibold">Machines</h2>
        <p className="mt-0.5 text-[13px] text-muted">
          Computers that ran <code>gban setup</code> or <code>gban doctor</code> and reported. A clean report
          is what turns “Connect your agent” ✓.
        </p>
      </div>
      {q.isError ? (
        <p role="alert" className="text-[13px] text-danger">Could not load machines: {errorDetail(q.error, "request failed")}</p>
      ) : q.isPending ? (
        <p className="text-[13px] text-muted">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-[13px] text-muted" data-testid="machines-empty">
          <span className="font-mono text-[11px] text-faint">○ NEVER REPORTED</span> — run{" "}
          <code>gban doctor</code> on a dev machine and it appears here.
        </p>
      ) : (
        <table className="w-full text-left text-[13px]">
          <thead className="font-mono text-[10px] uppercase tracking-wide text-faint">
            <tr>
              <th className="py-1.5 pr-3">Machine</th>
              <th className="pr-3">OS</th>
              <th className="pr-3">gban</th>
              <th className="pr-3">gbfleet</th>
              <th className="pr-3">Harnesses</th>
              <th className="pr-3">Last report</th>
              <th className="pr-3">State</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr key={m.id} className="border-t border-line" data-testid="machine-row">
                <td className="py-2 pr-3 font-semibold">{m.label || m.machine_id}</td>
                <td className="pr-3 text-fg-2">{m.os || "—"}</td>
                <td className="pr-3 font-mono text-[12px]">{m.gban_version || "—"}</td>
                <td className="pr-3 font-mono text-[12px]">{m.gbfleet_version || "—"}</td>
                <td className="pr-3 text-fg-2">{m.harnesses.join(", ") || "—"}</td>
                <td className="pr-3 text-fg-2">
                  {isStale(m)
                    ? `last seen ${new Date(m.last_reported_at).toLocaleDateString()}`
                    : relTime(m.last_reported_at) ?? "—"}
                </td>
                <td className="pr-3"><MachineState machine={m} /></td>
                <td className="text-right">
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={remove.isPending}
                    aria-label={`Remove ${m.label || m.machine_id}`}
                    onClick={() => remove.mutate(m.id)}
                  >
                    Remove
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {remove.isError ? (
        <p role="alert" className="text-[13px] text-danger">Could not remove: {errorDetail(remove.error, "request failed")}</p>
      ) : null}
    </div>
  );
}
