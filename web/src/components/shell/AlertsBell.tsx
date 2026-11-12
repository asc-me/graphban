import { Bell } from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { useMyAlerts, useReadAlert } from "@/lib/queries";

/**
 * The bell (GRPH-1527, P75 D4): unread alert count and a panel, newest first. A failed read
 * shows "could not load alerts" — never 0, which would tell a person nothing is waiting on them.
 */
export function AlertsBell() {
  const q = useMyAlerts();
  const read = useReadAlert();
  const [open, setOpen] = React.useState(false);
  const failed = q.isError && !q.data;
  const unread = q.data?.unread ?? 0;
  const label = failed ? "!" : q.data ? String(unread) : "…";

  return (
    <div className="relative">
      <button
        type="button"
        aria-label={failed ? "Alerts: could not load alerts" : q.data ? `Alerts, ${unread} unread` : "Alerts, loading"}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex h-9 items-center gap-1.5 rounded-lg border border-control bg-surface px-2.5 text-muted transition-colors hover:border-control-hover"
      >
        <Bell size={14} />
        <span data-testid="alerts-count" className="font-mono text-[11px]">
          {label}
        </span>
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Alerts"
          className="absolute right-0 top-11 z-50 max-h-[420px] w-[360px] overflow-auto rounded-lg border border-line bg-surface p-2 shadow-lg"
        >
          {failed && (
            <div role="alert" className="p-3 text-[13px] text-st-blocked">
              could not load alerts
              <button type="button" className="ml-2 underline" onClick={() => q.refetch()}>
                Retry
              </button>
            </div>
          )}
          {q.data && q.data.alerts.length === 0 && (
            <div className="p-3 text-[13px] text-muted">Nothing needs you.</div>
          )}
          {q.data?.alerts.map((a) => (
            <Link
              key={a.id}
              to={`/triage?item=${encodeURIComponent(a.ticket ?? a.wait)}`}
              onClick={() => {
                if (!a.read) read.mutate(a.id);
                setOpen(false);
              }}
              className="block rounded-md p-2.5 hover:bg-surface-2"
            >
              <div className={`text-[13px] ${a.read ? "text-fg-2" : "font-semibold"}`}>{a.question}</div>
              <div className="mt-0.5 font-mono text-[10.5px] text-faint">
                {[a.ticket, a.prd, a.asked_by && `asked by ${a.asked_by}`, a.created_at && new Date(a.created_at).toLocaleString()]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
