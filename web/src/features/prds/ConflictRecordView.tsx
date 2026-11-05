import * as React from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import type { ConflictDetail, SyncReport } from "@/lib/api";
import { useIntentDiff, usePrd } from "@/lib/queries";

/** Router state for this page: the sync report Open conflict / Open sync report came from. */
export interface ConflictRecordState {
  report?: SyncReport;
}

/** The page Open conflict / Open sync report open. Shows hashes and file facts only,
 * never either body. It reads the sync report from router state; a sync is a POST that
 * writes, so the page never runs one itself. Without a report it says so: a missing report
 * is not evidence that there is no conflict. */
export function ConflictRecordView() {
  const { id = "" } = useParams();
  const { state } = useLocation() as { state: ConflictRecordState | null };
  const report = state?.report;
  const conflict = report?.conflict_details?.find((c) => c.prd_id === id);
  const back = (
    <Link to={id ? `/prds/${id}` : "/prds"} className="text-small underline">
      Back to PRD
    </Link>
  );

  let body: React.ReactNode;
  if (!report) {
    body = (
      <p className="text-muted" data-testid="no-report">
        No sync report is open, so this page cannot say whether {id || "this PRD"} is in conflict. Run a sync
        from Specs and open the conflict from its report.
      </p>
    );
  } else if (conflict) {
    body = <ConflictFacts conflict={conflict} />;
  } else if (report.conflicts?.includes(id)) {
    // An older server lists the id without its detail: the conflict is real, the facts are not here.
    body = (
      <p className="text-muted" data-testid="conflict-no-detail">
        The sync report records a hash conflict for {id}, but carries no hashes or file facts for it.
      </p>
    );
  } else if (report.export_status === "failed") {
    body = (
      <p className="text-muted" data-testid="export-failed-not-conflict">
        The export of this sync failed. The ledger row for {id} is as it was, and no hash conflict is recorded
        for it. An export failure is not a conflict.
      </p>
    );
  } else {
    body = (
      <p className="text-muted" data-testid="no-conflict">
        This sync report records no hash conflict for {id}.
      </p>
    );
  }

  return (
    <div>
      <PlaceHeader viewName="Conflict record" purpose="Hashes and file facts for a sync conflict. Neither body is shown." />
      <div className="mx-auto max-w-[720px] space-y-4 p-4 text-[13px]">
        {body}
        {back}
      </div>
    </div>
  );
}

/** Statuses are listed as facts. There is deliberately no control that writes a body from
 * the file or the ledger: an approved or closed PRD takes no body from the folder. */
function ConflictFacts({ conflict }: { conflict: ConflictDetail }) {
  // The live PRD supplies the status (the report may be older) and pending rebaseline,
  // which is a separate field on the PRD, not a status value. Only `pending` is read from
  // the intent diff — its section lines are body text and are never rendered here.
  const prd = usePrd(conflict.prd_id);
  const diff = useIntentDiff(conflict.prd_id);
  const status = prd.data?.status ?? conflict.status;
  const facts: { label: string; current: boolean }[] = [
    { label: "approved", current: status === "approved" },
    { label: "closed", current: status === "closed" },
    { label: "pending rebaseline", current: !!diff.data?.pending },
  ];

  const rows: [string, React.ReactNode][] = [
    ["PRD id", conflict.prd_id],
    ["PRD status", status],
    ["Ledger hash", <code key="l">{conflict.ledger_hash}</code>],
    ["File hash", <code key="f">{conflict.file_hash}</code>],
    ["File name", conflict.file_name],
    ["Byte length", conflict.byte_length],
    ["Path", <code key="p">{conflict.path}</code>],
  ];

  return (
    <>
      <dl className="grid grid-cols-[140px_1fr] gap-y-1.5" data-testid="conflict-facts">
        {rows.map(([k, v]) => (
          <React.Fragment key={k}>
            <dt className="text-faint">{k}</dt>
            <dd className="break-all font-mono text-[12px]">{v}</dd>
          </React.Fragment>
        ))}
      </dl>
      <div>
        <div className="mb-1 text-faint">Status</div>
        <ul className="flex flex-wrap gap-2" aria-label="Status facts">
          {facts.map((f) => (
            <li
              key={f.label}
              className={`rounded-[8px] border px-2 py-0.5 text-small ${f.current ? "border-line-3 font-medium text-ink" : "border-line-2 text-faint"}`}
              aria-current={f.current ? "true" : undefined}
            >
              {f.label}
              {f.current ? " · current" : ""}
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
