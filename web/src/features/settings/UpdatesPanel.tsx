/**
 * This box → Updates. Three states; unknown is not current.
 * Check refetches. Install is enabled when `apply` is true and a cut is
 * available — compose apply is the host helper, not a Docker socket in the API.
 * After Install, the page polls `/api/platform/update-progress` for the steps (GRPH-1298) and
 * `/health` until this box serves the tag, says so, and reloads, because the old bundle is
 * still running in this tab. A failed install stops the poll and shows the log's error line.
 */
import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { AutoUpdateSettings } from "@/features/settings/AutoUpdateSettings";
import { Markdown } from "@/lib/markdown";
import { keys, useConfig, useUpdateCheck } from "@/lib/queries";
import type { ReleaseNotes, UpdateCheck, UpdateProgress } from "@/lib/types";

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
        <p className="text-lead font-semibold">On the latest release</p>
        <p className="text-[13px] text-fg-2">
          This box is on <Cut version={v} sha={sha} />.
        </p>
      </div>
    );
  }
  if (data.state === "available" && data.latest) {
    return (
      <div className="space-y-2">
        <p className="text-lead font-semibold">Update available</p>
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
      <p className="text-lead font-semibold">Could not check</p>
      <p className="text-[13px] text-fg-2">
        Could not tell whether a newer cut exists. This box reports <Cut version={v} sha={sha} />.
      </p>
      {data.feed_error ? (
        <p className="text-[12px] text-muted">Update feed: {data.feed_error}.</p>
      ) : null}
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
    return "Install rebuilds this box from the published cut. The build takes a few minutes while this release keeps serving; the API is down for a few seconds at the end.";
  }
  const tag = data.latest?.tag;
  return tag
    ? `Install from this page needs a compose host helper or a native /opt/graphban install. Until then: scripts/deploy.sh ${tag}`
    : "Install from this page needs a compose host helper or a native /opt/graphban install.";
}

/**
 * Install's clock, an object so tests can shrink it. Compose `up -d --build` rebuilds
 * both images while the old cut keeps serving — minutes, not the 90s this used to wait.
 */
export const installTiming = { pollMs: 3000, requestMs: 5000, budgetMs: 15 * 60_000, reloadMs: 1500 };

/** jsdom has no navigation; tests replace `run`. */
export const pageReload = { run: () => window.location.reload() };

type Phase = "" | "starting" | "building" | "restarting";

/**
 * A status is the API answering, so the apply was refused (403/409/502/503), not started.
 * No status is a dropped request — the apply recreating this process. Null means started.
 */
function refusal(err: unknown): string | null {
  const status = (err as { status?: unknown } | null)?.status;
  if (typeof status !== "number") return null;
  const raw = err instanceof Error ? err.message : "";
  try {
    const detail = JSON.parse(raw)?.detail;
    if (typeof detail === "string" && detail) return detail;
  } catch {
    /* not a FastAPI body */
  }
  return raw || `HTTP ${status}`;
}

function elapsed(startedAt: number | null, nowMs: number): string {
  if (startedAt === null) return "";
  const sec = Math.max(0, Math.floor(nowMs / 1000 - startedAt));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

const MARK: Record<string, string> = { pending: "○", active: "●", done: "✓", failed: "✗" };

function ProgressSteps({ progress, nowMs }: { progress: UpdateProgress; nowMs: number }) {
  if (!progress.steps.length) return null;
  return (
    <div className="mt-2 space-y-1">
      <ol className="space-y-0.5 text-[12px]" aria-label="Install steps">
        {progress.steps.map((st) => (
          <li
            key={st.key}
            data-status={st.status}
            className={st.status === "failed" ? "text-danger" : st.status === "pending" ? "text-faint" : "text-fg-2"}
          >
            <span aria-hidden className="mr-1.5 font-mono">{MARK[st.status]}</span>
            {st.label}
            <span className="sr-only"> ({st.status})</span>
            {st.status === "active" && st.started_at !== null ? (
              <span className="ml-1.5 font-mono text-faint" data-testid="step-timer">
                {elapsed(st.started_at, nowMs)}
              </span>
            ) : null}
          </li>
        ))}
      </ol>
      {progress.current_line ? (
        <p className="truncate font-mono text-small text-faint" title={progress.current_line}>
          {progress.current_line}
        </p>
      ) : null}
    </div>
  );
}

function PhaseCopy({ phase, tag, from }: { phase: Phase; tag: string; from: string }) {
  if (!phase) return null;
  const text = phase === "starting"
    ? "Starting the install…"
    : phase === "building"
      ? `Building ${tag}. This box keeps serving ${from} until the new cut is ready — this can take several minutes.`
      : `Restarting the API on ${tag}…`;
  return <p className="mt-2 text-[12px] text-fg-2">{text}</p>;
}

export function UpdatesPanel() {
  const { data: config } = useConfig();
  const { data, isError, isPending, isFetching } = useUpdateCheck();
  const qc = useQueryClient();
  // The button bypasses the server cache; the initial load and reloads use it.
  const refresh = () => qc.fetchQuery({
    queryKey: keys.updateCheck, queryFn: () => api.updateCheck(true), staleTime: 0,
  }).catch(() => undefined);
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
  const [phase, setPhase] = React.useState<Phase>("");
  const [installed, setInstalled] = React.useState("");
  const [progress, setProgress] = React.useState<UpdateProgress | null>(null);
  const [nowMs, setNowMs] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (!applying) return;
    const t = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(t);
  }, [applying]);
  const alive = React.useRef(true);
  React.useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  async function install() {
    if (!tag) return;
    const via = data?.via;
    setApplying(true);
    setApplyError("");
    setPhase("starting");
    setProgress(null);
    try {
      await api.updateApply(tag);
    } catch (err) {
      const why = refusal(err);
      if (why !== null) {
        setApplying(false);
        setPhase("");
        setApplyError(`Install was refused: ${why}`);
        return;
      }
      // No status: deploy.sh recreates the API container, so a dropped request is the apply starting.
    }
    // Poll /health, not update-check: every update-check asks GitHub, and a 15-minute
    // poll of it spends the unauthenticated limit and then reads `unknown` forever.
    const deadline = Date.now() + installTiming.budgetMs;
    while (alive.current && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, installTiming.pollMs));
      if (!alive.current) return;
      let failed: UpdateProgress | null = null;
      let progressDown = false;
      try {
        const got = await api.updateProgress(installTiming.requestMs);
        if (!alive.current) return;
        // A section for another tag is an older attempt, not this install: treat it as
        // not started yet rather than showing (or failing on) its steps.
        if (got.tag === tag) {
          setProgress(got);
          setNowMs(Date.now());
          if (got.state === "failed") failed = got;
        } else {
          setProgress(null);
        }
      } catch {
        progressDown = true; // the API is being recreated; /health below says whether it is back
      }
      if (failed) {
        setApplying(false);
        setPhase("");
        const where = via === "compose"
          ? "The full log is deploy.log beside the helper's socket on the host (~/.graphban-apply/deploy.log by default)."
          : "The full log is upgrade.log under the install root on the host (/opt/graphban/upgrade.log).";
        setApplyError(`Install failed: ${failed.error || "see the log"}. ${where}`);
        return;
      }
      try {
        const health = await api.health(installTiming.requestMs);
        if (health.version === tag) {
          setPhase("");
          setApplying(false);
          setConfirming(false);
          setInstalled(tag);
          void refresh();
          // The web container was rebuilt too; this tab is still running the old bundle.
          setTimeout(() => { if (alive.current) pageReload.run(); }, installTiming.reloadMs);
          return;
        }
        setPhase(progressDown ? "restarting" : "building");
      } catch {
        setPhase("restarting");
      }
    }
    if (!alive.current) return;
    setApplying(false);
    setPhase("");
    const minutes = Math.round(installTiming.budgetMs / 60_000);
    setApplyError(
      `This box is still not serving ${tag} after ${minutes} minutes. The apply may have failed — `
      + (via === "compose"
        ? "the helper appends deploy.sh output to deploy.log beside its socket (~/.graphban-apply/deploy.log by default) on the host."
        : "check graphban_host.py upgrade on the host."),
    );
  }

  return (
    <div className="max-w-2xl">
      <div className="text-lead font-semibold">Updates</div>
      <p className="mb-3 mt-0.5 text-body text-muted">
        Whether this box is on the published stable cut.
      </p>
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <Label>Status</Label>
        {isPending && !data && <p className="text-[13px] text-muted">Checking GitHub for a newer release — this can take a few seconds. No result yet is not "no release".</p>}
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
            onClick={() => { void refresh(); }}
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
        {!hosted && !installed && (
          <p className="mt-2 text-[12px] text-muted">{installHint(data)}</p>
        )}
        <div role="status" aria-live="polite">
          <PhaseCopy phase={phase} tag={tag} from={data?.running.version ?? ""} />
          {applying && progress ? <ProgressSteps progress={progress} nowMs={nowMs} /> : null}
          {installed ? (
            <p className="mt-2 text-[13px] font-semibold text-fg">
              Installed {installed}. Reloading this page to load the new release…
            </p>
          ) : null}
        </div>
        {applyError ? <p className="mt-2 text-[12px] text-danger">{applyError}</p> : null}
      </div>
      <AutoUpdateSettings progressError={progress?.state === "failed" ? progress.error : ""} />
    </div>
  );
}
