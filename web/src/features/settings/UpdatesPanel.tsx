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
import { AutoUpdateSettings, StepBar, stepCount } from "@/features/settings/AutoUpdateSettings";
import { Markdown } from "@/lib/markdown";
import { useQuery } from "@tanstack/react-query";
import { keys, useAutoUpdate, useConfig, useUpdateCheck } from "@/lib/queries";
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
        <h3 className="text-lead font-semibold">
          Update available <span className="font-mono">{data.latest.tag}</span>
        </h3>
        <p className="text-[13px] text-fg-2">
          Running <Cut version={v} sha={sha} />.
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
      {data.feed_error ? (
        <p className="text-[13px] text-fg-2">
          {data.feed_error}{/[.!?]$/.test(data.feed_error) ? "" : "."}
        </p>
      ) : (
        <p className="text-[13px] text-fg-2">
          Could not tell whether a newer cut exists. This box reports <Cut version={v} sha={sha} />.
        </p>
      )}
      {data.note ? <p className="text-[12px] text-muted">{data.note}</p> : null}
    </div>
  );
}

/** Top-level "- " bullet lines in a notes body. Notes carry no categories, so no chips. */
export function changeCount(body: string): number {
  return body.split("\n").filter((line) => line.startsWith("- ")).length;
}

/** A "- #1301 Title (GRPH-1315)" line split into its PR number, ticket ids and remaining title. */
export function parseCall(line: string): { pr: string; tickets: string[]; title: string } {
  let rest = line.replace(/^-\s+/, "");
  const m = /^(#\d+)\s*/.exec(rest);
  const pr = m ? m[1] : "";
  if (m) rest = rest.slice(m[0].length);
  const tickets = rest.match(/\b[A-Z][A-Z0-9]*-\d+\b/g) ?? [];
  let title = rest;
  for (const t of tickets) title = title.replace(t, "");
  title = title.replace(/\(\s*[,\s]*\)/g, "").replace(/\s{2,}/g, " ").trim();
  // "items=GRPH-1329" with the id lifted out leaves a bare "items=" behind; drop it.
  if (tickets.length) title = title.replace(/\s*\bitems=[,\s]*$/, "").trim();
  return { pr, tickets, title };
}

/** Body of a release note: the "Since <prev>" list as pill rows, the rest as markdown. */
function NotesBody({ body }: { body: string }) {
  const lines = body.split("\n");
  const at = lines.findIndex((l) => /^\*\*Since\b.*\*\*\s*$/.test(l.trim()));
  if (at < 0) return <Markdown source={body} />;
  let end = at + 1;
  while (end < lines.length && (lines[end].trim() === "" || lines[end].startsWith("- "))) end++;
  const calls = lines.slice(at + 1, end).filter((l) => l.startsWith("- "));
  if (!calls.length) return <Markdown source={body} />;
  const before = lines.slice(0, at + 1).join("\n");
  const after = lines.slice(end).join("\n");
  return (
    <>
      <Markdown source={before} />
      <ul className="u-calls" aria-label="Changes">
        {calls.map((l, i) => {
          const c = parseCall(l);
          const wip = /^WIP: salvaged/.test(c.title);
          return (
            <li key={i} className="u-call" data-testid="release-call" data-pr={c.pr || undefined}>
              {c.pr ? <span className="u-pill text-muted">{c.pr}</span> : null}
              {c.tickets.map((t) => <span key={t} className="u-pill text-accent">{t}</span>)}
              <span className={`u-call-title ${wip ? "text-faint" : ""}`}>{c.title}</span>
            </li>
          );
        })}
      </ul>
      {after.trim() ? <Markdown source={after} /> : null}
    </>
  );
}

/** One collapsed row per release, "N changes" at the right, expanding to the body. */
function NotesAccordion({ label, notes }: { label: string; notes: ReleaseNotes }) {
  const changes = notes.state === "present" ? changeCount(notes.body) : 0;
  return (
    <details className="rounded-[10px] border border-line-2 bg-surface p-3.5">
      <summary className="flex cursor-pointer select-none items-center justify-between gap-2 font-mono text-[10.5px] uppercase tracking-wide text-faint">
        <span>
          <span aria-hidden className="u-chev mr-1.5 inline-block">▸</span>
          {label}{" "}
          <span className="font-mono normal-case tracking-normal text-fg">{notes.tag}</span>
        </span>
        {changes > 0 ? (
          <span
            data-testid="changes-pill"
            className="rounded-full border border-line-2 px-1.5 py-px font-mono normal-case tracking-normal text-fg-2"
          >
            {changes} {changes === 1 ? "change" : "changes"}
          </span>
        ) : null}
      </summary>
      <div className="mt-2 text-[13px] leading-relaxed text-fg-2">
        {notes.state === "unknown" ? (
          <p>Could not load notes for this release.</p>
        ) : null}
        {notes.state === "empty" ? (
          <p>No notes on this release.</p>
        ) : null}
        {notes.state === "present" ? <NotesBody body={notes.body} /> : null}
      </div>
    </details>
  );
}

function ReleaseNotesList({ data }: { data: UpdateCheck }) {
  const running = data.notes?.running;
  const latest = data.notes?.latest;
  if (!running) return null;
  // Unknown latest notes (a failed check) is no notes block at all.
  if (data.state === "unknown" || (data.state === "available" && latest?.state === "unknown")) return null;
  const showLatest = data.state === "available" && latest && latest.tag !== running.tag;
  return (
    <div className="mt-4 space-y-2">
      <Label>Release notes</Label>
      {showLatest ? <NotesAccordion label="Latest release" notes={latest} /> : null}
      <NotesAccordion label="This release" notes={running} />
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
  if (data.apply || data.hosted) {
    return "";
  }
  // No apply path on a self-hosted box: still say how to install by hand.
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

/**
 * The progress header. A native install's log lists only the steps it has printed, so there
 * is no known total: no "of M", and the count says so (GRPH-1302). Never invent pending steps.
 */
function progressHeadline(progress: UpdateProgress, tag: string, via: string | undefined): string {
  const steps = progress.steps;
  const failedStep = steps.find((st) => st.status === "failed");
  if (failedStep) return `Upgrade to ${tag} stopped at ${failedStep.label.toLowerCase()}`;
  if (progress.state === "failed") return `Upgrade to ${tag} failed`;
  const activeIdx = steps.findIndex((st) => st.status === "active");
  const done = steps.filter((st) => st.status === "done").length;
  const n = activeIdx >= 0 ? activeIdx + 1 : Math.min(done + 1, steps.length);
  return via === "native"
    ? `Upgrading to ${tag} — step ${n}`
    : `Upgrading to ${tag} — step ${n} of ${steps.length}`;
}

function ProgressSteps({ progress, nowMs, tag, via, complete = false }: {
  progress: UpdateProgress; nowMs: number; tag: string; via: string | undefined; complete?: boolean;
}) {
  const failed = progress.state === "failed";
  if (!progress.steps.length && !failed) return null;
  const steps = progress.steps;
  const red = failed || steps.some((st) => st.status === "failed");
  const count = stepCount(steps, via);
  const native = via === "native";
  return (
    <div className="mt-3 space-y-2">
      <div className="flex items-center justify-between gap-2 text-[13px] font-semibold text-fg">
        <span className="flex items-center gap-2" data-testid="progress-headline">
          <span aria-hidden className={`h-[7px] w-[7px] rounded-full ${red ? "bg-danger" : complete ? "bg-st-done" : "u-dot-live bg-accent"}`} />
          {complete ? "Upgrade complete" : progressHeadline(progress, tag, via)}
        </span>
        <span className="font-mono text-[12px] font-normal text-fg-2" data-testid="step-count">{count}</span>
      </div>
      {steps.length ? <StepBar steps={steps} openEnded={native} /> : null}
      {steps.length ? <div className="rounded-[10px] border border-line-2 bg-surface py-1">
        <ol className="text-[12px]" aria-label="Install steps">
          {steps.map((st) => (
            <li
              key={st.key}
              data-status={st.status}
              className={`u-step flex min-h-8 items-center px-3 ${
                st.status === "failed" ? "text-danger" : st.status === "pending" ? "text-faint" : "text-fg-2"}`}
            >
              <span aria-hidden className={`u-mk mr-2.5 w-3.5 flex-none text-center font-mono text-[12px] ${
                st.status === "done" ? "text-st-done" : st.status === "active" ? "text-accent"
                  : st.status === "failed" ? "text-danger" : "text-faint"}`}>{MARK[st.status]}</span>
              <span className={st.status === "active" ? "font-medium text-fg" : ""}>{st.label}</span>
              <span className="sr-only"> ({st.status})</span>
              {st.status === "active" && st.started_at !== null ? (
                <span className="ml-auto font-mono text-[11.5px] text-faint" data-testid="step-timer">
                  {elapsed(st.started_at, nowMs)}
                </span>
              ) : null}
            </li>
          ))}
          {native && progress.state === "running" && !complete ? (
            <li className="flex min-h-8 items-center italic text-muted">Further steps appear only if the log prints them</li>
          ) : null}
        </ol>
      </div> : null}
      {progress.current_line ? (
        <p className="truncate font-mono text-small text-faint" title={progress.current_line}>
          {progress.current_line}
        </p>
      ) : null}
    </div>
  );
}

function PhaseCopy({ phase, tag, from, via }: { phase: Phase; tag: string; from: string; via?: string }) {
  if (!phase) return null;
  // Native (frame 5b): the compose sentences describe image builds this path never runs.
  if (via === "native") {
    return (
      <>
        <p className="mt-2 text-[12px] text-fg-2" data-testid="phase-copy">Upgrading to <span className="font-mono">{tag}</span>.</p>
        <p className="mt-1 text-[12px] text-muted" data-testid="native-note">
          The service restarts mid-upgrade, so this panel reconnects partway through. The steps
          below were read back from the log after the restart; they don&apos;t tick live the way
          Compose does.
        </p>
      </>
    );
  }
  const t = <span className="font-mono">{tag}</span>;
  const text = phase === "starting"
    ? "Starting the install…"
    : phase === "building"
      ? <>Building {t}. This box keeps serving <span className="font-mono">{from}</span> until the new cut is ready — this can take several minutes.</>
      : <>Restarting the API on {t}…</>;
  return <p className="mt-2 text-[12px] text-fg-2" data-testid="phase-copy">{text}</p>;
}

/** Whether the card may animate: matchMedia exists and the user has not asked for reduced motion. */
function motionAllowed(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** How long the old card content fades out before the new content mounts (the design's 220ms). */
export const SWAP_FADE_MS = 220;

/**
 * The design's swap(). When the card's view changes, the old content stays mounted (frozen as it
 * last rendered, inert) and fades out over 220ms. Then the new content mounts, the card's height
 * animates from the old height to the new one (.45s), and the new content enters from opacity 0
 * and translateY(4px). Reduced motion swaps at once with no animation.
 */
function useCardSwap(ref: React.RefObject<HTMLDivElement | null>, view: string, content: React.ReactNode) {
  const [shown, setShown] = React.useState(view);
  const frozen = React.useRef<React.ReactNode>(content);
  const fromHeight = React.useRef(0);
  const animate = motionAllowed();
  const fading = shown !== view && animate;

  // The last content drawn outside a fade is what fades out when the view next changes.
  React.useLayoutEffect(() => {
    if (!fading) frozen.current = content;
  });

  React.useEffect(() => {
    if (shown === view) return;
    if (!motionAllowed()) { setShown(view); return; }
    fromHeight.current = ref.current?.offsetHeight ?? 0;
    const t = setTimeout(() => setShown(view), SWAP_FADE_MS);
    return () => clearTimeout(t);
  }, [ref, view, shown]);

  const first = React.useRef(true);
  React.useLayoutEffect(() => {
    if (first.current) { first.current = false; return; }
    const card = ref.current;
    const body = card?.firstElementChild as HTMLElement | null;
    if (!card || !body || !motionAllowed() || !fromHeight.current) return;
    const h1 = card.offsetHeight;
    card.style.height = `${fromHeight.current}px`;
    card.classList.add("u-card-anim");
    body.classList.add("u-enter");
    void card.offsetHeight;
    card.style.height = `${h1}px`;
    const raf = requestAnimationFrame(() => body.classList.remove("u-enter"));
    const done = setTimeout(() => {
      card.style.height = "";
      card.classList.remove("u-card-anim");
    }, 480);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(done);
      card.style.height = "";
      card.classList.remove("u-card-anim");
      body.classList.remove("u-enter");
    };
  }, [ref, shown]);

  return (
    <div
      className={`u-swap${fading ? " u-fading" : ""}`}
      data-testid="card-body"
      data-view={fading ? shown : view}
      inert={fading}
      aria-hidden={fading ? true : undefined}
    >
      {fading ? frozen.current : content}
    </div>
  );
}

export function UpdatesPanel() {
  const { data: config } = useConfig();
  const { data, isError, isPending, isFetching } = useUpdateCheck();
  const qc = useQueryClient();
  // The button bypasses the server cache; the initial load and reloads use it.
  const refresh = () => qc.fetchQuery({
    queryKey: keys.updateCheck, queryFn: () => api.updateCheck(true), staleTime: 0,
  }).catch(() => undefined);
  const hosted = Boolean(config?.hosted_mode || data?.hosted);
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
  const [installTag, setInstallTag] = React.useState("");
  const [progress, setProgress] = React.useState<UpdateProgress | null>(null);
  const [nowMs, setNowMs] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (!applying) return;
    const t = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(t);
  }, [applying]);
  const { data: auto } = useAutoUpdate();
  const autoRunning = auto?.last_auto_install_outcome === "started" && !applying;
  const { data: autoProgress } = useQuery({
    queryKey: ["update-progress", "auto"],
    queryFn: () => api.updateProgress(installTiming.requestMs),
    enabled: autoRunning,
    refetchInterval: installTiming.pollMs,
    retry: false,
  });
  const autoInstalling = autoRunning && autoProgress?.state === "running"
    && autoProgress.tag === auto?.last_auto_install_tag;
  const alive = React.useRef(true);
  React.useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const resumed = React.useRef(false);
  React.useEffect(() => {
    if (!data || resumed.current) return;
    resumed.current = true;
    void (async () => {
      let got: UpdateProgress;
      try {
        got = await api.updateProgress(installTiming.requestMs);
      } catch {
        return;
      }
      if (!alive.current || got.state !== "running" || !got.tag) return;
      setInstallTag(got.tag);
      setProgress(got);
      setNowMs(Date.now());
      setApplying(true);
      setPhase("building");
      await pollInstall(got.tag, data.via);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  async function install() {
    if (!tag) return;
    const via = data?.via;
    setApplying(true);
    setApplyError("");
    setPhase("starting");
    setProgress(null);
    setInstallTag(tag);
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
    await pollInstall(tag, via);
  }

  /** The poll an Install starts, and the one a page opened mid-install resumes. */
  async function pollInstall(tag: string, via: string | undefined) {
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
        setConfirming(false);
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
          setProgress((p) => p && {
            ...p, state: "succeeded" as UpdateProgress["state"],
            steps: p.steps.map((st) => ({ ...st, status: "done" as typeof st.status })),
          });
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

  const view = installed ? "complete" : applying || autoInstalling ? "installing" : "plain";
  const cardRef = React.useRef<HTMLDivElement>(null);
  const showInstall = !hosted && !installed && !autoInstalling;
  const cardBody = (
    <>
      {applying ? <Label>Status</Label> : <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <Label>Status</Label>
          {isPending && !data && <p className="text-[13px] text-muted">Checking GitHub for a newer release — this can take a few seconds. No result yet is not "no release".</p>}
          {isError && !data && (
            <p className="text-[13px] text-fg-2">
              Could not tell whether a newer cut exists. The check did not complete — not current.
            </p>
          )}
          {data && <StateCopy data={data} />}
        </div>
        <div className="flex flex-col items-end gap-1">
          <div className="u-act justify-end" data-testid="install-group" data-confirm={showInstall && confirming ? "true" : "false"}>
            {!installed ? (
              <span className="u-slot u-slot-check" data-testid="slot-check" inert={showInstall && confirming} aria-hidden={showInstall && confirming ? true : undefined}>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => { void refresh(); }}
                  disabled={isFetching || applying}
                >
                  {isFetching && !applying ? "Checking…" : "Check for updates"}
                </Button>
              </span>
            ) : null}
            {showInstall ? (
              <Button
                type="button"
                className="u-install"
                disabled={confirming ? applying || !tag : !canInstall || applying}
                onClick={() => { if (confirming) void install(); else setConfirming(true); }}
              >
                <span className="u-lbl u-lbl-a" aria-hidden={confirming ? true : undefined}>
                  {applying ? "Installing…" : "Install"}
                </span>
                <span className="u-lbl u-lbl-b" aria-hidden={confirming ? undefined : true}>
                  {applying ? "Installing…" : `Confirm install ${tag}`}
                </span>
              </Button>
            ) : null}
            {showInstall ? (
              <span className="u-slot u-slot-cancel" data-testid="slot-cancel" inert={!confirming} aria-hidden={confirming ? undefined : true}>
                <Button
                  type="button"
                  variant="ghost"
                  disabled={applying}
                  onClick={() => { setConfirming(false); setApplyError(""); }}
                >
                  Cancel
                </Button>
              </span>
            ) : null}
          </div>
          {!hosted && !installed && !autoInstalling && (
            installHint(data) ? <p className="max-w-xs text-right text-[12px] text-muted">{installHint(data)}</p> : null
          )}
        </div>
      </div>}
      <div role="status" aria-live="polite">
        <PhaseCopy phase={phase} tag={installTag || tag} from={data?.running.version ?? ""} via={data?.via} />
        {(applying || installed || progress?.state === "failed") && progress
          ? <ProgressSteps progress={progress} nowMs={nowMs} tag={installTag || tag} via={data?.via} complete={Boolean(installed)} />
          : autoInstalling && autoProgress
            ? <ProgressSteps progress={autoProgress} nowMs={nowMs} tag={auto?.last_auto_install_tag ?? ""} via={data?.via} />
            : null}
        {installed ? (
          <p className="mt-2 text-[13px] font-semibold text-fg">
            Installed {installed}. Reloading this page to load the new release…
          </p>
        ) : null}
      </div>
      {applyError ? <p className="mt-2 text-[12px] text-danger">{applyError}</p> : null}
      {data && (
        <div className="mt-4">
          <Label>How this box is deployed</Label>
          <p className="text-[13px] text-fg-2">{methodCopy(data)}</p>
        </div>
      )}
      {data && !applying ? <ReleaseNotesList data={data} /> : null}
    </>
  );
  const swap = useCardSwap(cardRef, view, cardBody);
  return (
    <div className="max-w-2xl">
      <div className="text-lead font-semibold">Updates</div>
      <p className="mb-3 mt-0.5 text-body text-muted">
        Whether this box is on the published stable cut.
      </p>
      <div ref={cardRef} className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        {swap}
      </div>
      <AutoUpdateSettings
        progressError={progress?.state === "failed" ? progress.error : ""}
        progress={applying ? progress : autoProgress ?? null}
        via={data?.via}
      />
    </div>
  );
}
