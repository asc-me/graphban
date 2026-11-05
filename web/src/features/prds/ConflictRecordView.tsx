import * as React from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import type { ConflictDetail } from "@/lib/api";

/** Statuses are listed as facts. There is deliberately no control that writes a body
 * from the file or the ledger: an approved or closed PRD takes no body from the folder. */
const STATUS_FACTS = ["approved", "closed", "pending rebaseline"];

/** The page Open conflict / Open sync report open. Shows hashes and file facts only,
 * never either body. Reached with the sync report's conflict_details in router state. */
export function ConflictRecordView() {
  const { id = "" } = useParams();
  const { state } = useLocation() as { state: { conflicts?: ConflictDetail[] } | null };
  const conflict = state?.conflicts?.find((c) => c.prd_id === id);

  const rows: [string, React.ReactNode][] = conflict
    ? [
        ["PRD id", conflict.prd_id],
        ["PRD status", conflict.status],
        ["Ledger hash", <code key="l">{conflict.ledger_hash}</code>],
        ["File hash", <code key="f">{conflict.file_hash}</code>],
        ["File name", conflict.file_name],
        ["Byte length", conflict.byte_length],
        ["Path", <code key="p">{conflict.path}</code>],
      ]
    : [];

  return (
    <div>
      <PlaceHeader viewName="Conflict record" purpose="Hashes and file facts for a sync conflict. Neither body is shown." />
      <div className="mx-auto max-w-[720px] space-y-4 p-4 text-[13px]">
        {!conflict ? (
          // An export failure with the ledger row intact is not a conflict, so it has no record.
          <p className="text-muted" data-testid="no-conflict">
            No hash conflict is recorded for {id || "this PRD"}. An export failure alone is not a conflict.
          </p>
        ) : (
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
                {STATUS_FACTS.map((s) => (
                  <li
                    key={s}
                    className="rounded-[8px] border border-line-2 px-2 py-0.5 text-small"
                    aria-current={s === conflict.status ? "true" : undefined}
                  >
                    {s}
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
        <Link to={id ? `/prds/${id}` : "/prds"} className="text-small underline">
          Back to PRD
        </Link>
      </div>
    </div>
  );
}
