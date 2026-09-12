import { Link } from "react-router-dom";

import { cn } from "@/lib/cn";
import type { FleetMatrixRow } from "@/lib/types";

/**
 * Committed harness × model × tier catalog (GRPH-866).
 *
 * Facts only. The Observe → Harness page is where rates and rankings live; this table
 * names what can run, and a link is how you get to how it turned out.
 */
export function MatrixTable({ rows, harnessHref }: {
  rows: FleetMatrixRow[];
  harnessHref: string;
}) {
  return (
    <section className="mb-7" data-testid="fleet-matrix">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 className="text-[14px] font-semibold tracking-tight">Harness catalog</h2>
          <p className="mt-0.5 text-[12px] text-muted">
            What <code className="font-mono text-[11px]">spawn(tier=)</code> can resolve to.
            Status moves by a commit, not by this page.
          </p>
        </div>
        <Link
          to={harnessHref}
          className="shrink-0 text-[12px] text-fg-2 underline-offset-2 hover:underline"
          data-testid="fleet-matrix-observe"
        >
          Performance and rankings
        </Link>
      </div>
      {rows.length === 0 ? (
        <p className="rounded-[11px] border border-dashed border-line-2 px-3 py-6 text-center text-[12.5px] text-muted">
          The catalog has not been served. That is not an empty matrix.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-[11px] border border-line-2">
          <table className="w-full text-left text-[12.5px]">
            <thead className="border-b border-line-2 bg-surface-2 font-mono text-[10px] uppercase tracking-wide text-faint">
              <tr>
                <th className="px-3 py-2 font-medium">Harness</th>
                <th className="px-3 py-2 font-medium">Model</th>
                <th className="px-3 py-2 font-medium">Tier</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Cost</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.harness}:${row.model}:${row.tier}`}
                    className="border-b border-line-2 last:border-0">
                  <td className="px-3 py-2 font-mono text-[12px]">{row.harness}</td>
                  <td className="px-3 py-2 font-mono text-[12px] text-muted">
                    {row.model || "—"}
                  </td>
                  <td className="px-3 py-2 font-mono text-[12px]">{row.tier}</td>
                  <td className="px-3 py-2">
                    <span className={cn(
                      "rounded-md border px-1.5 py-0.5 font-mono text-[10px]",
                      row.status === "verified" && "border-accent/40 text-accent",
                      row.status === "failed" && "border-[color:var(--color-st-blocked)]/40 text-[color:var(--color-st-blocked)]",
                      row.status === "unregistered" && "border-line-2 text-faint",
                      (row.status === "unverified" || !["verified", "failed", "unregistered"].includes(row.status))
                        && "border-line-2 text-muted",
                    )}>
                      {row.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-muted">
                    {row.cost_class}{row.local ? " · local" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
