/**
 * This box → Updates. Check only (P32). Three states; unknown is not current.
 * There is no Apply button in this slice.
 */
import * as React from "react";

import { useUpdateCheck } from "@/lib/queries";
import type { UpdateCheck } from "@/lib/types";

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

function StateCopy({ data }: { data: UpdateCheck }) {
  const v = data.running.version;
  const sha = data.running.git_sha;
  if (data.state === "current") {
    return (
      <p className="text-[13px] text-fg-2">
        This instance is on <span className="font-mono text-fg">{v}</span>
        {sha && sha !== "unknown" ? <span className="font-mono text-faint"> ({sha})</span> : null}.
      </p>
    );
  }
  if (data.state === "available" && data.latest) {
    return (
      <div className="space-y-2">
        <p className="text-[13px] text-fg-2">
          <span className="font-mono text-fg">{data.latest.tag}</span> is available. This
          instance is on <span className="font-mono">{v}</span>
          {sha && sha !== "unknown" ? <span className="font-mono text-faint"> ({sha})</span> : null}.
        </p>
        {data.latest.url ? (
          <a
            className="text-[12.5px] text-accent underline-offset-2 hover:underline"
            href={data.latest.url}
            rel="noopener noreferrer"
            target="_blank"
          >
            Release notes
          </a>
        ) : null}
        <p className="text-[12px] text-muted">
          {data.hosted
            ? "This hosted instance is updated by the operator, not from this page."
            : "Compose: scripts/deploy.sh " + data.latest.tag + ". Native apply is not on this page yet."}
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-[13px] text-fg-2">
        Could not tell whether a newer cut exists. This instance reports{" "}
        <span className="font-mono">{v}</span>
        {sha && sha !== "unknown" ? <span className="font-mono text-faint"> ({sha})</span> : null}.
      </p>
      {data.note ? <p className="text-[12px] text-muted">{data.note}</p> : null}
    </div>
  );
}

export function UpdatesPanel() {
  const { data, isError, isPending } = useUpdateCheck();
  return (
    <div className="max-w-2xl">
      <div className="text-[14px] font-semibold">Updates</div>
      <p className="mb-3 mt-0.5 text-[12.5px] text-muted">
        Whether this box is on the published stable cut. Checking is not applying.
      </p>
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <Label>This instance</Label>
        {isPending && <p className="text-[13px] text-muted">Checking…</p>}
        {isError && (
          <p className="text-[13px] text-fg-2">
            Could not tell whether a newer cut exists. The check did not complete — not current.
          </p>
        )}
        {data && <StateCopy data={data} />}
      </div>
    </div>
  );
}
