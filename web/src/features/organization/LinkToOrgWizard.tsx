import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { errorDetail } from "@/lib/errors";
import { CONSOLE_URL } from "@/lib/routes";
import type {
  CloudProject,
  LinkStartResult,
  ProjectPushResult,
  SyncProjectState,
  SyncStatus,
} from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * The Link to an org wizard (GRPH-P67 §5): ① Console · ② Sign in · ③ Projects · ④ First push.
 * It replaces the paste-a-key form. No screen here asks for, shows or mints a key.
 */
export type WizardStep = 1 | 2 | 3 | 4;

const RAIL = ["Console", "Sign in", "Projects", "First push"] as const;
const LOCAL = "";
const SEAT_REMOVED = ["seat_removed", "token_revoked", "key_refused"];

export function tagOf(p: CloudProject | undefined, fallback: string): string {
  return (p?.name || fallback).toUpperCase();
}

/** The first unfinished step, from `/api/sync/status`. */
export function resumeStep(status: SyncStatus): WizardStep {
  if (!status.signed_in) return 1;
  const mapped = status.projects.filter((p) => p.mapped);
  if (mapped.length === 0) return 3;
  if (mapped.some((p) => !p.last_push_result)) return 4;
  return 4;
}

function refusalText(reason: string | undefined, org: string): string {
  switch (reason) {
    case "declined":
      return "The code was declined in the console.";
    case "expired":
      return "This code expired. Get a new code.";
    case "not_member":
      return "That account isn't a member of an org.";
    case "no_seat":
      return `You don't have a seat in ${org || "this org"}. Ask an org admin.`;
    case "seat_removed":
    case "token_revoked":
      return `Your seat in ${org || "this org"} was removed. Nothing is syncing.`;
    case "key_refused":
      return "This link used a key. Sign in to keep syncing.";
    default:
      return reason || "The sign-in was refused.";
  }
}

type Line = { text: string; tone: "wait" | "ok" | "red" | "grey" | "muted" };

export function LinkToOrgWizard({
  status,
  initialStep,
  readOnly = false,
  onLinked,
  onDone,
}: {
  status: SyncStatus;
  initialStep?: WizardStep;
  readOnly?: boolean;
  onLinked?: () => void;
  onDone?: () => void;
}) {
  const [step, setStep] = React.useState<WizardStep>(initialStep ?? resumeStep(status));
  // The env URL comes from console_url_env, set even on an unlinked box (GRPH-1537).
  const envUrl = status.console_url_env ?? "";
  const [url, setUrl] = React.useState(envUrl || status.cloud_url || CONSOLE_URL);
  const [start, setStart] = React.useState<LinkStartResult | null>(null);
  const [signIn, setSignIn] = React.useState<
    "waiting" | "approved" | "refused" | "unreachable"
  >("waiting");
  const [refusal, setRefusal] = React.useState<string | undefined>();
  const [org, setOrg] = React.useState(status.org);
  const [email, setEmail] = React.useState(status.linked_email ?? "");
  const [copied, setCopied] = React.useState("");
  const [err, setErr] = React.useState<string | null>(null);

  async function begin() {
    setErr(null);
    setStart(null);
    setSignIn("waiting");
    setStep(2);
    try {
      const r = await api.syncLinkStart(envUrl || url.trim());
      if (r.status === "started") setStart(r);
      else if (r.status === "refused") {
        setSignIn("refused");
        setRefusal(r.reason);
      } else setSignIn("unreachable");
    } catch (e) {
      setErr(errorDetail(e, "Could not start the sign-in."));
      setSignIn("unreachable");
    }
  }

  // Poll while a code is on screen and nothing has answered yet.
  React.useEffect(() => {
    if (step !== 2 || !start || signIn !== "waiting") return;
    let live = true;
    const ms = Math.max(10, (start.interval ?? 3) * 1000);
    const t = window.setInterval(async () => {
      try {
        const r = await api.syncLinkPoll();
        if (!live) return;
        if (r.status === "approved") {
          setOrg(r.org ?? "");
          setEmail(r.email ?? "");
          setSignIn("approved");
          onLinked?.();
        } else if (r.status === "refused") {
          setSignIn("refused");
          setRefusal(r.reason);
        } else if (r.status === "unknown") setSignIn("unreachable");
      } catch {
        if (live) setSignIn("unreachable");
      }
    }, ms);
    return () => {
      live = false;
      window.clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, start, signIn]);

  function copy(label: string, text: string) {
    void navigator.clipboard?.writeText(text);
    setCopied(label);
  }

  // ③ Projects
  const [cloud, setCloud] = React.useState<{
    state: "loading" | "ok" | "refused" | "unknown";
    projects: CloudProject[];
    reason?: string;
  }>({ state: "loading", projects: [] });
  const [choice, setChoice] = React.useState<Record<string, string>>(() =>
    Object.fromEntries(
      status.projects.filter((p) => p.mapped).map((p) => [p.project_id, p.cloud_project_id ?? LOCAL]),
    ),
  );
  const [pushes, setPushes] = React.useState<ProjectPushResult[] | null>(null);
  const [pushing, setPushing] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  const loadCloud = React.useCallback(async () => {
    setCloud({ state: "loading", projects: [] });
    try {
      const r = await api.syncCloudProjects();
      setCloud({ state: r.status, projects: r.projects ?? [], reason: r.reason });
    } catch {
      setCloud({ state: "unknown", projects: [] });
    }
  }, []);
  React.useEffect(() => {
    if (step === 3) void loadCloud();
  }, [step, loadCloud]);

  const synced = Object.values(choice).filter(Boolean);
  const nameOf = (id: string) =>
    tagOf(cloud.projects.find((p) => p.id === id), id);

  async function saveAndPush() {
    setSaving(true);
    setErr(null);
    try {
      await api.syncSaveProjectMap(
        Object.entries(choice)
          .filter(([, c]) => c)
          .map(([local_project_id, cloud_project_id]) => ({ local_project_id, cloud_project_id })),
      );
      setStep(4);
    } catch (e) {
      setErr(errorDetail(e, "Could not save the project choices."));
    } finally {
      setSaving(false);
    }
  }

  async function push() {
    setPushing(true);
    setErr(null);
    try {
      const r = await api.syncPushAll();
      setPushes(r.projects);
    } catch (e) {
      setErr(errorDetail(e, "Could not push."));
    } finally {
      setPushing(false);
    }
  }

  const seatRemoved = !!pushes?.length && pushes.every(
    (p) => p.status === "refused" && SEAT_REMOVED.includes(p.reason ?? ""),
  );
  const done = pushes?.filter((p) => p.status === "done").length ?? 0;
  const refused = pushes?.filter((p) => p.status === "refused").length ?? 0;

  let line: Line;
  const cloudSeatRefused = step === 3 && cloud.state === "refused" && SEAT_REMOVED.includes(cloud.reason ?? "");
  if ((step <= 2 && signIn === "refused") || cloudSeatRefused) line = { text: "SIGN-IN · REFUSED", tone: "red" };
  else if (signIn === "unreachable" || (step === 3 && cloud.state === "unknown"))
    line = { text: "CONSOLE · UNREACHABLE", tone: "grey" };
  else if (step === 1 || (step === 2 && signIn === "waiting"))
    line = { text: "SIGN-IN · WAITING", tone: "wait" };
  else if (step === 2) line = { text: `SIGNED IN · ${org.toUpperCase()}`, tone: "ok" };
  else if (step === 3)
    line = {
      text: `PROJECTS · ${synced.length} SYNCED · ${status.projects.length - synced.length} LOCAL`,
      // Green only once something syncs; nothing chosen yet is muted, not done.
      tone: synced.length > 0 ? "ok" : "muted",
    };
  else if (!pushes) line = { text: "PUSH · NOT YET", tone: pushing ? "wait" : "muted" };
  else line = { text: `PUSH · ${done} DONE · ${refused} REFUSED`, tone: refused ? "red" : "ok" };
  const pulsing = line.tone === "wait";

  const orgLabel = org || status.org || "the org";

  return (
    <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4" data-testid="link-wizard">
      <ol className="mb-3 flex flex-wrap gap-x-4 gap-y-1 font-mono text-small" aria-label="Link steps">
        {RAIL.map((label, i) => (
          <li
            key={label}
            aria-current={step === i + 1 ? "step" : undefined}
            className={cn(step === i + 1 ? "text-accent" : "text-faint")}
          >
            {["①", "②", "③", "④"][i]} {label}
          </li>
        ))}
      </ol>
      <div
        role="status"
        data-tone={line.tone}
        className={cn(
          "mb-4 flex items-center gap-2 font-mono text-[10px] uppercase tracking-wide",
          line.tone === "red" && "text-st-blocked",
          line.tone === "ok" && "text-st-done",
          (line.tone === "grey" || line.tone === "muted") && "text-faint",
          // Waiting is muted (the dot pulses); never lime or amber — Org Link.dc.html.
          line.tone === "wait" && "text-muted",
        )}
      >
        <span
          data-testid="status-dot"
          data-pulse={pulsing ? "true" : "false"}
          className={cn("h-1.5 w-1.5 rounded-full bg-current", pulsing && "animate-pulse motion-reduce:animate-none")}
        />
        {line.text}
      </div>

      {readOnly && (
        <p className="mb-3 text-small text-faint">
          Only a box admin can link this deployment. You can see where it stands here.
        </p>
      )}

      {step === 1 && (
        <div className="space-y-3">
          <label className="block text-small text-muted" htmlFor="console-url">
            Console URL
          </label>
          <Input
            id="console-url"
            value={envUrl || url}
            readOnly={!!envUrl || readOnly}
            onChange={(e) => setUrl(e.target.value)}
          />
          {envUrl && (
            <p className="text-small text-faint">
              Set by <code className="font-mono text-[10.5px]">SYNC_CLOUD_URL</code> on this
              deployment.
            </p>
          )}
          <Button size="sm" onClick={begin} disabled={readOnly || !(envUrl || url.trim())}>
            Get a sign-in code
          </Button>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-3">
          {start && signIn === "waiting" && (
            <>
              <div className="font-mono text-[22px] tracking-[0.2em]" data-testid="user-code">
                {start.user_code}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="ghost" onClick={() => copy("code", start.user_code ?? "")}>
                  Copy
                </Button>
                <a
                  className="inline-flex h-8 items-center rounded-[8px] border border-control px-3 text-[12px]"
                  href={start.verification_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open the console →
                </a>
                <Button size="sm" variant="ghost" onClick={() => copy("link", start.verification_url ?? "")}>
                  Copy link
                </Button>
              </div>
              {copied && <span className="sr-only">Copied {copied}</span>}
              <p className="text-small text-faint">Expires in 10 minutes.</p>
              <p className="text-small text-faint">
                Or in a terminal: <code className="font-mono text-small">graphban link</code>
              </p>
            </>
          )}
          {signIn === "approved" && (
            <>
              <p className="text-body text-st-done">
                Signed in to {orgLabel}
                {email ? ` as ${email}` : ""}.
              </p>
              <Button size="sm" onClick={() => setStep(3)}>
                Choose projects →
              </Button>
            </>
          )}
          {signIn === "refused" && (
            <>
              <p className="text-body text-st-blocked">{refusalText(refusal, orgLabel)}</p>
              <Button size="sm" onClick={begin}>
                Get a new code
              </Button>
            </>
          )}
          {signIn === "unreachable" && (
            <>
              <p className="text-body text-faint">
                {err ?? "The console could not be reached."}
              </p>
              <Button size="sm" onClick={begin}>
                Try again
              </Button>
            </>
          )}
        </div>
      )}

      {step === 3 && (
        <div className="space-y-3">
          {cloud.state === "loading" && <p className="text-body text-faint">Reading projects…</p>}
          {cloud.state === "unknown" && (
            <>
              <p className="text-body text-faint">The console could not be reached.</p>
              <Button size="sm" onClick={() => void loadCloud()}>
                Try again
              </Button>
            </>
          )}
          {cloud.state === "refused" && (
            <>
              <p className="text-body text-st-blocked">{refusalText(cloud.reason, orgLabel)}</p>
              {SEAT_REMOVED.includes(cloud.reason ?? "") && (
                <Button size="sm" onClick={begin} disabled={readOnly}>
                  Sign in again
                </Button>
              )}
            </>
          )}
          {cloud.state === "ok" && cloud.projects.length === 0 && (
            <p className="text-body text-muted">
              You don't have access to any projects in {orgLabel} yet. Ask an org admin.
            </p>
          )}
          {cloud.state === "ok" &&
            status.projects.map((p: SyncProjectState) => {
              const value = choice[p.project_id] ?? LOCAL;
              return (
                <div key={p.project_id} className="flex items-center gap-3">
                  <span className="min-w-0 flex-1 truncate text-body">{p.name}</span>
                  <select
                    aria-label={`Cloud project for ${p.name}`}
                    value={value}
                    disabled={readOnly}
                    onChange={(e) => setChoice({ ...choice, [p.project_id]: e.target.value })}
                    className="h-8 rounded-[8px] border border-control bg-surface-2 px-2 text-[12px]"
                  >
                    <option value={LOCAL}>Local only</option>
                    {cloud.projects.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                  <span className="w-24 font-mono text-[10px] text-faint">
                    {value ? `☁ ${nameOf(value)}` : "local"}
                  </span>
                </div>
              );
            })}
          {cloud.state === "ok" && synced.length === 0 && (
            <p className="text-small text-faint">
              Nothing will sync. The box stays linked to {orgLabel}.
            </p>
          )}
          {err && <p className="text-small text-st-blocked">{err}</p>}
          {cloud.state === "ok" && (
            <Button size="sm" onClick={saveAndPush} disabled={readOnly || saving}>
              {synced.length === 0 ? "Finish" : "Continue"}
            </Button>
          )}
        </div>
      )}

      {step === 4 && (
        <div className="space-y-3">
          {seatRemoved && (
            <p className="text-body text-st-blocked">
              Your seat in {orgLabel} was removed. Nothing is syncing.
            </p>
          )}
          {status.projects
            .filter((p) => p.mapped || choice[p.project_id])
            .map((p) => {
              const r = pushes?.find((x) => x.project_id === p.project_id);
              const tag = nameOf(choice[p.project_id] ?? p.cloud_project_id ?? "");
              return (
                <div key={p.project_id} className="flex items-baseline gap-3 text-body">
                  <span className="w-32 truncate">{p.name}</span>
                  {!r && <span className="text-faint">☁ {tag}</span>}
                  {r?.status === "done" && (
                    <span className="text-st-done">
                      Pushed {(r.nodes ?? 0).toLocaleString("en-US")} nodes and{" "}
                      {(r.edges ?? 0).toLocaleString("en-US")} edges to {tag}
                      {r.at ? ` at ${new Date(r.at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : ""}.
                    </span>
                  )}
                  {r?.status === "refused" && (
                    <span className="text-st-blocked">
                      {seatRemoved ? "☁ REFUSED" : `No access to ${tag}. Ask an org admin.`}
                    </span>
                  )}
                  {r?.status === "unknown" && (
                    <span className="text-faint">☁ UNKNOWN — the console did not answer.</span>
                  )}
                </div>
              );
            })}
          {seatRemoved && (
            <p className="text-small text-faint">
              Anyone with a seat in {orgLabel} can sign this box in. Your project choices are kept
              and checked again.
            </p>
          )}
          {err && <p className="text-small text-st-blocked">{err}</p>}
          <div className="flex items-center gap-3">
            {seatRemoved ? (
              <Button size="sm" onClick={begin} disabled={readOnly}>
                Sign in again
              </Button>
            ) : (
              <Button size="sm" onClick={push} disabled={readOnly || pushing}>
                {pushing ? "Pushing…" : pushes ? "Push again" : "Push now"}
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={onDone}>
              Push later
            </Button>
            <span className="text-small text-faint">
              Later: <code className="font-mono text-small">graphban sync</code>
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
