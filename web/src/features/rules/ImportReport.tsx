/**
 * The import report (GRPH-1951, P92 D3). Unread sources first, then findings grouped by stage and
 * bucket, each with its quote and source link. A person ticks proposed findings and adopts them as
 * one batch under one reason; nothing is pre-ticked. Members see it read-only.
 *
 * A rescan (GRPH-1953, P92 D5) adds drift notices: `source gone` (an adopted rule nothing at the
 * new commit still supports) and `source moved` (the same rule, cited from another file). They
 * are shown with no checkbox: nothing files, nothing changes, and only a person loosens a rule.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { errorDetail } from "@/lib/errors";
import { useAdoptImportRun, useImportRun, useImportRuns } from "@/lib/queries";
import type { ImportFinding } from "@/lib/types";

import { useCanWriteRules } from "./LibraryRuleControls";

export const BUCKET_LABEL: Record<ImportFinding["bucket"], string> = {
  proposed: "Proposed rules",
  external: "EXTERNAL — held outside Graphban, not verified",
  gap: "Guidance (no primitive yet)",
  notice: "Drift (shown, never acted on)",
};
const BUCKETS: ImportFinding["bucket"][] = ["proposed", "external", "gap", "notice"];

function Report({ projectId, runId }: { projectId: string; runId: number }) {
  const q = useImportRun(projectId, runId);
  const adopt = useAdoptImportRun(projectId, runId);
  const canWrite = useCanWriteRules(projectId);
  const [ticked, setTicked] = React.useState<Set<number>>(new Set());
  const [reason, setReason] = React.useState("");
  const [err, setErr] = React.useState<string | null>(null);
  const run = q.data;
  if (!run) return null;

  const stages = Array.from(new Set(run.findings.map((f) => f.stage)));
  const toggle = (id: number) =>
    setTicked((t) => {
      const n = new Set(t);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const submit = () => {
    setErr(null);
    adopt.mutate(
      { ids: [...ticked], reason },
      { onSuccess: () => { setTicked(new Set()); setReason(""); }, onError: (e) => setErr(errorDetail(e, "adoption refused")) },
    );
  };

  return (
    <div data-testid="import-report" className="space-y-3 text-sm">
      <div className="font-mono text-xs text-muted-foreground">
        import run {run.id} · commit {run.commit.slice(0, 8)} · by {run.created_by}
      </div>
      {run.sources_unread.length > 0 && (
        <section aria-label="Unread sources">
          <h4 className="font-medium">Unread sources</h4>
          <ul>
            {run.sources_unread.map((s) => (
              <li key={s.path} className="font-mono text-xs">
                {s.path}{s.reason ? ` — ${s.reason}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
      {stages.map((stage) => (
        <section key={stage} aria-label={`Stage ${stage}`}>
          <h4 className="font-medium">{stage}</h4>
          {BUCKETS.map((b) => {
            const fs = run.findings.filter((f) => f.stage === stage && f.bucket === b);
            if (!fs.length) return null;
            return (
              <div key={b} className="ml-3">
                <h5 className="text-xs uppercase text-muted-foreground">{BUCKET_LABEL[b]}</h5>
                <ul className="space-y-1">
                  {fs.map((f) => (
                    <li key={f.id} className="flex items-start gap-2">
                      {b === "proposed" && canWrite && f.proposal?.status === "open" && (
                        <input type="checkbox" aria-label={`Adopt finding ${f.id}`} checked={ticked.has(f.id)} onChange={() => toggle(f.id)} />
                      )}
                      <span>
                        {b === "notice" && f.label && <strong className="mr-1">{f.label}:</strong>}
                        <q>{f.quote}</q>{" "}
                        {f.source.url ? (
                          <a className="underline" href={f.source.url} target="_blank" rel="noreferrer">{f.source.path}</a>
                        ) : (
                          <span className="font-mono">{f.source.path}</span>
                        )}
                        {f.text && b !== "proposed" && <span className="text-muted-foreground"> — {f.text}</span>}
                        {f.proposal && f.proposal.status !== "open" && <span> · {f.proposal.status}</span>}
                        {f.refused && <span className="text-destructive"> · refused: {f.refused}</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </section>
      ))}
      {canWrite && (
        <div className="flex items-center gap-2">
          <Input aria-label="Reason for the batch" placeholder="One reason for the batch" value={reason} onChange={(e) => setReason(e.target.value)} />
          <Button disabled={!ticked.size || !reason.trim() || adopt.isPending} onClick={submit}>
            Adopt {ticked.size || ""} selected
          </Button>
        </div>
      )}
      {err && <p role="alert" className="text-destructive">{err}</p>}
    </div>
  );
}

/** The newest import run's report, or nothing when the project has never imported. */
export function ImportReport() {
  const { activeId } = useProjectCtx();
  const runs = useImportRuns(activeId ?? "");
  const latest = runs.data?.[0];
  if (!activeId || !latest) return null;
  return <Report projectId={activeId} runId={latest.id} />;
}
