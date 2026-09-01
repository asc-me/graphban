/**
 * This box → Updates. Three states; unknown is not current.
 * Check refetches. Install is present on self-host and stays disabled until
 * `apply` is true — Compose has no in-API apply (P32: host runs deploy.sh).
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { useConfig, useUpdateCheck } from "@/lib/queries";
import type { UpdateCheck } from "@/lib/types";

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

function Cut({ version, sha }: { version: string; sha: string }) {
  return (
    <>
      <span className="font-mono text-fg">{version}</span>
      {sha && sha !== "unknown" ? <span className="font-mono text-faint"> ({sha})</span> : null}
    </>
  );
}

function StateCopy({ data }: { data: UpdateCheck }) {
  const v = data.running.version;
  const sha = data.running.git_sha;
  if (data.state === "current") {
    return (
      <div className="space-y-1">
        <p className="text-[14px] font-semibold">On the latest release</p>
        <p className="text-[13px] text-fg-2">
          This box is on <Cut version={v} sha={sha} />.
        </p>
      </div>
    );
  }
  if (data.state === "available" && data.latest) {
    return (
      <div className="space-y-2">
        <p className="text-[14px] font-semibold">Update available</p>
        <p className="text-[13px] text-fg-2">
          <span className="font-mono text-fg">{data.latest.tag}</span> is available. This
          box is on <Cut version={v} sha={sha} />.
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
        {data.hosted ? (
          <p className="text-[12px] text-muted">
            This hosted instance is updated by the operator, not from this page.
          </p>
        ) : null}
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-[14px] font-semibold">Could not check</p>
      <p className="text-[13px] text-fg-2">
        Could not tell whether a newer cut exists. This box reports <Cut version={v} sha={sha} />.
      </p>
      {data.note ? <p className="text-[12px] text-muted">{data.note}</p> : null}
    </div>
  );
}

function installHint(data: UpdateCheck | undefined): string {
  if (!data || data.state === "unknown") {
    return "Install is disabled until a check succeeds.";
  }
  if (data.state === "current") {
    return "Install is disabled — this box is already on the latest release.";
  }
  if (data.apply) return "";
  const tag = data.latest?.tag;
  return tag
    ? `Install from this page is not available for Compose. On the host: scripts/deploy.sh ${tag}`
    : "Install from this page is not available for Compose yet.";
}

export function UpdatesPanel() {
  const { data: config } = useConfig();
  const { data, isError, isPending, isFetching, refetch } = useUpdateCheck();
  const hosted = config?.hosted_mode ?? data?.hosted ?? false;
  const canInstall = Boolean(data?.apply && data.state === "available" && !hosted);

  return (
    <div className="max-w-2xl">
      <div className="text-[14px] font-semibold">Updates</div>
      <p className="mb-3 mt-0.5 text-[12.5px] text-muted">
        Whether this box is on the published stable cut.
      </p>
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <Label>Status</Label>
        {isPending && !data && <p className="text-[13px] text-muted">Checking…</p>}
        {isError && !data && (
          <p className="text-[13px] text-fg-2">
            Could not tell whether a newer cut exists. The check did not complete — not current.
          </p>
        )}
        {data && <StateCopy data={data} />}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => { void refetch(); }}
            disabled={isFetching}
          >
            {isFetching ? "Checking…" : "Check for updates"}
          </Button>
          {!hosted && (
            <Button type="button" disabled={!canInstall}>
              Install
            </Button>
          )}
        </div>
        {!hosted && (
          <p className="mt-2 text-[12px] text-muted">{installHint(data)}</p>
        )}
      </div>
    </div>
  );
}
