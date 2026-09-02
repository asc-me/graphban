/**
 * This box → Updates. Three states; unknown is not current.
 * Check refetches. Install is enabled when `apply` is true and a cut is
 * available — compose apply is the host helper, not a Docker socket in the API.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { Markdown } from "@/lib/markdown";
import { useConfig, useUpdateCheck } from "@/lib/queries";
import type { ReleaseNotes, UpdateCheck } from "@/lib/types";

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

function NotesAccordion({
  label,
  notes,
  defaultOpen,
}: {
  label: string;
  notes: ReleaseNotes;
  defaultOpen: boolean;
}) {
  return (
    <details
      className="rounded-[10px] border border-line-2 bg-surface p-3.5"
      open={defaultOpen}
    >
      <summary className="cursor-pointer select-none font-mono text-[10.5px] uppercase tracking-wide text-faint">
        {label}{" "}
        <span className="font-mono normal-case tracking-normal text-fg">{notes.tag}</span>
      </summary>
      <div className="mt-2 text-[13px] leading-relaxed text-fg-2">
        {notes.state === "unknown" ? (
          <p>Could not load notes for this release.</p>
        ) : null}
        {notes.state === "empty" ? (
          <p>No notes on this release.</p>
        ) : null}
        {notes.state === "present" ? <Markdown source={notes.body} /> : null}
      </div>
    </details>
  );
}

function ReleaseNotesList({ data }: { data: UpdateCheck }) {
  const running = data.notes?.running;
  const latest = data.notes?.latest;
  if (!running) return null;
  const showLatest = data.state === "available" && latest && latest.tag !== running.tag;
  return (
    <div className="mt-4 space-y-2">
      <Label>Release notes</Label>
      <NotesAccordion
        label="This release"
        notes={running}
        defaultOpen={!showLatest}
      />
      {showLatest ? (
        <NotesAccordion label="Latest release" notes={latest} defaultOpen />
      ) : null}
    </div>
  );
}

function methodCopy(data: UpdateCheck): string {
  if (data.hosted) {
    return "Hosted. The operator updates this instance — not compose.";
  }
  if (data.via === "native") {
    return "Native install at /opt/graphban.";
  }
  if (data.via === "compose") {
    return "Compose, with a host helper on the unix socket.";
  }
  return "Could not tell — not compose.";
}

function installHint(data: UpdateCheck | undefined): string {
  if (!data || data.state === "unknown") {
    return "Install is disabled until a check succeeds.";
  }
  if (data.state === "current") {
    return "Install is disabled — this box is already on the latest release.";
  }
  if (data.apply && data.via === "native") {
    return "Install unpacks the published tarball and swaps /opt/graphban/current. The API will be down for a few seconds.";
  }
  if (data.apply) {
    return "Install rebuilds this box from the published cut. The API will be down for a few seconds.";
  }
  const tag = data.latest?.tag;
  return tag
    ? `Install from this page needs a compose host helper or a native /opt/graphban install. Until then: scripts/deploy.sh ${tag}`
    : "Install from this page needs a compose host helper or a native /opt/graphban install.";
}

export function UpdatesPanel() {
  const { data: config } = useConfig();
  const { data, isError, isPending, isFetching, refetch } = useUpdateCheck();
  const hosted = config?.hosted_mode ?? data?.hosted ?? false;
  const canInstall = Boolean(
    data?.apply
    && data.state === "available"
    && !hosted
    && (data.via === "compose" || data.via === "native"),
  );
  const tag = data?.latest?.tag ?? "";
  const [confirming, setConfirming] = React.useState(false);
  const [applying, setApplying] = React.useState(false);
  const [applyError, setApplyError] = React.useState("");

  async function install() {
    if (!tag) return;
    setApplying(true);
    setApplyError("");
    try {
      await api.updateApply(tag);
    } catch {
      // deploy.sh recreates the API container; a dropped request is the apply starting.
    }
    for (let i = 0; i < 45; i++) {
      try {
        const next = await refetch();
        if (next.data?.state === "current") {
          setApplying(false);
          setConfirming(false);
          return;
        }
      } catch {
        /* API is down during rebuild */
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    setApplying(false);
    setApplyError("The helper started the apply, but this box did not come back on the new cut.");
  }

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
        {data && (
          <div className="mt-4">
            <Label>How this box is deployed</Label>
            <p className="text-[13px] text-fg-2">{methodCopy(data)}</p>
          </div>
        )}
        {data && <ReleaseNotesList data={data} />}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => { void refetch(); }}
            disabled={isFetching || applying}
          >
            {isFetching && !applying ? "Checking…" : "Check for updates"}
          </Button>
          {!hosted && !confirming && (
            <Button
              type="button"
              disabled={!canInstall || applying}
              onClick={() => setConfirming(true)}
            >
              {applying ? "Installing…" : "Install"}
            </Button>
          )}
          {!hosted && confirming && (
            <>
              <Button type="button" disabled={applying || !tag} onClick={() => { void install(); }}>
                {applying ? "Installing…" : `Confirm install ${tag}`}
              </Button>
              <Button
                type="button"
                variant="ghost"
                disabled={applying}
                onClick={() => { setConfirming(false); setApplyError(""); }}
              >
                Cancel
              </Button>
            </>
          )}
        </div>
        {!hosted && (
          <p className="mt-2 text-[12px] text-muted">{installHint(data)}</p>
        )}
        {applyError ? <p className="mt-2 text-[12px] text-danger">{applyError}</p> : null}
      </div>
    </div>
  );
}
