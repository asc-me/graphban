import * as React from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { RowSyncState, SyncPathFact, SyncReport, SyncStorageStatus } from "@/lib/api";
import { useRunSyncStorage, useSaveSyncStorage } from "@/lib/queries";

import type { ConflictRecordState } from "./ConflictRecordView";

/** Row sync state words. None uses the approved color: "in sync" is not "approved". Each is
 * readable from its word alone; color only marks the two that need a person. */
export const ROW_SYNC_META: Record<RowSyncState, { label: string; className: string }> = {
  in_sync: { label: "in sync", className: "text-muted" },
  not_exported: { label: "not exported", className: "text-faint" },
  ledger_changed: { label: "ledger newer", className: "text-faint" },
  file_changed: { label: "file newer", className: "text-faint" },
  conflict: { label: "conflict", className: "text-danger" },
  read_failed: { label: "read failed", className: "text-danger" },
  unknown: { label: "sync unknown", className: "text-faint" },
};

export function rowSyncMeta(state: string): { label: string; className: string } {
  return ROW_SYNC_META[state as RowSyncState] ?? { label: state, className: "text-faint" };
}

interface Props {
  projectId: string;
  data: SyncStorageStatus | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
}

/** Sync storage, a panel on Specs (GRPH-1320). Three facts: the folder path, the blob
 * directory, the index. A failure on one fact is shown on that fact; it never clears the
 * documents listed above. */
export function SyncStoragePanel({ projectId, data, isLoading, isError, onRetry }: Props) {
  const navigate = useNavigate();
  const save = useSaveSyncStorage(projectId);
  const run = useRunSyncStorage(projectId);
  // The report of a sync this panel just ran; otherwise the read-only preview.
  const [ranReport, setRanReport] = React.useState<SyncReport | null>(null);
  const report = ranReport ?? data?.report ?? null;

  function openReport(prdId: string) {
    if (!report) return;
    const state: ConflictRecordState = { report };
    navigate(`/prds/conflict/${prdId}`, { state });
  }

  async function runSync() {
    const out = await run.mutateAsync();
    setRanReport(out.run ?? null);
  }

  return (
    <section aria-labelledby="sync-storage-title" className="mx-4 mb-4 rounded-[12px] border border-line-2 bg-surface-2 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="sync-storage-title" className="text-[13.5px] font-medium text-fg">
          Sync storage
        </h2>
        <div className="ml-auto flex gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!report || report.conflicts.length === 0}
            onClick={() => report && openReport(report.conflicts[0])}
          >
            Open sync report
          </Button>
          <Button size="sm" onClick={runSync} disabled={run.isPending || !data}>
            {run.isPending ? "Syncing…" : "Run sync"}
          </Button>
        </div>
      </div>

      <p className="mt-2 text-small text-muted">
        An approved PRD whose file does not match the ledger stays a conflict. A failed export does not roll the
        ledger back.
      </p>

      {isLoading ? (
        <p className="mt-3 text-small text-faint">Reading sync storage…</p>
      ) : isError || !data ? (
        <p className="mt-3 text-small text-danger" role="alert">
          Sync storage could not be read. The documents above are unaffected.{" "}
          <button type="button" className="underline" onClick={onRetry}>
            Retry sync storage
          </button>
        </p>
      ) : (
        <>
          <dl className="mt-3 grid gap-3">
            <PathFact
              testId="fact-folder"
              label="Folder path"
              help="A folder on this box, not an account. Point it at a Google Drive Desktop folder; that is how Drive is reached."
              fact={data.folder}
              extra={data.folder.project_path ? `This project syncs ${data.folder.project_path}/PRDs` : undefined}
              saving={save.isPending}
              onSave={(v) => save.mutateAsync({ folder_path: v })}
            />
            <PathFact
              testId="fact-blobs"
              label="Blob directory"
              help="A local path. Bytes are immutable and keyed by sha256; the ledger row is the record. Changing it does not move bytes already stored."
              fact={data.blob_dir}
              saving={save.isPending}
              onSave={(v) => save.mutateAsync({ blob_dir: v })}
            />
            <div data-testid="fact-index">
              <dt className="text-small font-medium text-fg-2">Index</dt>
              <dd className="mt-0.5 text-small">
                {data.index.available ? (
                  <span className="text-muted">
                    available
                    {data.index.mode === "lexical" ? " · matches words only (no embedder)" : ""}
                  </span>
                ) : (
                  <span className="text-danger">unavailable: {data.index.reason}</span>
                )}
                <span className="block text-faint">An unavailable index does not hide the ledger rows.</span>
              </dd>
            </div>
          </dl>

          {save.isError && (
            <p className="mt-2 text-small text-danger" role="alert">
              Not saved: {(save.error as Error).message}
            </p>
          )}
          {run.isError && (
            <p className="mt-2 text-small text-danger" role="alert">
              Sync did not run: {(run.error as Error).message}
            </p>
          )}

          <LastRun data={data} />

          {report && report.read_failures && report.read_failures.length > 0 && (
            <div className="mt-3" data-testid="read-failures">
              <p className="text-small font-medium text-fg-2">Read failures (not conflicts)</p>
              <ul className="mt-1 space-y-0.5 text-small">
                {report.read_failures.map((f) => (
                  <li key={f.path}>
                    <span className="font-mono text-meta">{f.path}</span>
                    {f.prd_id && <span className="text-faint"> · {f.prd_id}</span>}
                    <span className="text-danger"> · {f.error}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {report && report.conflicts.length > 0 && (
            <div className="mt-3" data-testid="sync-conflicts">
              <p className="text-small font-medium text-fg-2">Hash conflicts</p>
              <ul className="mt-1 space-y-0.5 text-small">
                {report.conflicts.map((id) => (
                  <li key={id}>
                    <span className="font-mono text-meta">{id}</span>{" "}
                    <button type="button" className="underline" onClick={() => openReport(id)}>
                      Open conflict
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function LastRun({ data }: { data: SyncStorageStatus }) {
  const lr = data.last_run;
  if (!lr.at) return <p className="mt-3 text-small text-faint">Sync has not run on this box.</p>;
  const when = new Date(lr.at).toLocaleString();
  let what: React.ReactNode;
  if (lr.export_status === "failed") {
    what = <span className="text-danger">export failed; the ledger was left as it was</span>;
  } else if (lr.export_status === "not_run") {
    what = <span className="text-danger">did not run: {lr.error}</span>;
  } else {
    what = <span className="text-muted">export ok</span>;
  }
  return (
    <p className="mt-3 text-small" data-testid="last-run">
      <span className="text-faint">Last sync {when}: </span>
      {what}
    </p>
  );
}

function PathFact({
  testId,
  label,
  help,
  fact,
  extra,
  saving,
  onSave,
}: {
  testId: string;
  label: string;
  help: string;
  fact: SyncPathFact;
  extra?: string;
  saving: boolean;
  onSave: (value: string) => Promise<unknown>;
}) {
  const [editing, setEditing] = React.useState(false);
  const [value, setValue] = React.useState(fact.configured ? fact.path : "");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    try {
      await onSave(value.trim());
      setEditing(false);
    } catch {
      // The panel shows the refusal; keep the field open with what was typed.
    }
  }

  return (
    <div data-testid={testId}>
      <dt className="text-small font-medium text-fg-2">{label}</dt>
      <dd className="mt-0.5 text-small">
        {editing ? (
          <form onSubmit={submit} className="flex gap-2">
            <Input
              aria-label={label}
              value={value}
              placeholder={fact.configured ? "" : fact.path}
              onChange={(e) => setValue(e.target.value)}
              autoFocus
            />
            <Button type="submit" size="sm" disabled={saving}>
              Save
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </form>
        ) : (
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-meta text-fg-2">{fact.path}</span>
            {!fact.configured && <span className="text-faint">(default)</span>}
            {fact.ok ? (
              <span className="text-muted">readable{fact.writable ? "" : ", not writable"}</span>
            ) : (
              <span className="text-danger" role="status">
                cannot read: {fact.error}
              </span>
            )}
            <button
              type="button"
              className="text-faint underline hover:text-fg-2"
              onClick={() => {
                setValue(fact.configured ? fact.path : "");
                setEditing(true);
              }}
            >
              Change
            </button>
          </span>
        )}
        {extra && <span className="block text-faint">{extra}</span>}
        <span className="block text-faint">{help}</span>
      </dd>
    </div>
  );
}
