import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { cloudTagText } from "@/components/shell/CloudTag";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { noteGitopsUnlinked } from "@/features/settings/GitopsPanel";
import { api } from "@/lib/api";
import { errorDetail } from "@/lib/errors";
import { useProjects } from "@/lib/queries";
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

/** The ☁ tag for a cloud project: its console tag, else the stored one, else `?` — never the
 * id and never the name (GRPH-1562). */
export function tagOf(p: CloudProject | undefined, stored?: string | null): string {
  return cloudTagText(p?.tag || stored);
}

/** Where "③ finished with nothing chosen" is remembered. Saving an empty project map leaves
 *  no row on the server, so the box cannot tell "never chose" from "chose nothing"; this
 *  browser remembers it until Unlink clears it (GRPH-1563). */
export const LINK_FINISHED_KEY = "graphban.link.finished";

export function linkFinished(): boolean {
  try {
    return window.localStorage.getItem(LINK_FINISHED_KEY) === "1";
  } catch {
    return false;
  }
}

export function setLinkFinished(on: boolean) {
  try {
    if (on) window.localStorage.setItem(LINK_FINISHED_KEY, "1");
    else window.localStorage.removeItem(LINK_FINISHED_KEY);
  } catch {
    /* storage unavailable: resume falls back to ③ */
  }
}

/** The first unfinished step (design L3 "Resume"), or `null` for the Linked window.
 *  - no token → ② (① only when no console URL is known and no sign-in is pending)
 *  - signed in, nothing mapped, wizard never finished → ③
 *  - mapped but never pushed → ④
 *  - otherwise → Linked.
 *  A key link (`legacy`, or a key baked in by env) is linked and still syncing: it opens the
 *  Linked window, whose relink line offers Sign in again. */
export function resumeTarget(status: SyncStatus, finished = linkFinished()): WizardStep | null {
  if (!status.signed_in) {
    // A link that still holds a credential (a migrated key, or one baked in by env) is
    // linked and syncing — that is the Linked window, not a missing token.
    if (status.linked && (status.legacy || status.credential_set)) return null;
    return status.cloud_url || status.sign_in_pending ? 2 : 1;
  }
  const mapped = status.projects.filter((p) => p.mapped);
  if (mapped.length === 0) return finished ? null : 3;
  if (mapped.every((p) => !p.last_push_result)) return 4;
  return null;
}

/**
 * The one link controller (GRPH-1564). Settings → Sync / Link and the Organization window both
 * run it off `GET /api/sync/status`, so the two can never disagree on whether the box is linked,
 * which step is unfinished, or what pushed. `wizard` is true while the wizard should show.
 */
export function useLinkFlow(status: SyncStatus | undefined, fallbackReadOnly = false) {
  const qc = useQueryClient();
  const [step, setStep] = React.useState<WizardStep | null>(null);
  // Done closes the wizard for this visit even before the refetched status lands.
  const [closed, setClosed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const resume = status ? resumeTarget(status) : null;
  const wizard = !!status && (step !== null || (resume !== null && !(closed && status.signed_in)));
  // Only a box admin may link, re-point or unlink; can_link answers it on a linked AND an
  // unlinked box (GRPH-1537). An older API without it falls back to the caller's answer.
  const readOnly = status?.can_link === false || (status?.can_link === undefined && fallbackReadOnly);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["sync-status"] });
    qc.invalidateQueries({ queryKey: ["sync-org"] });
    qc.invalidateQueries({ queryKey: ["platform"] });
  };
  // Prefix of keys.gitops(id). Link is instance-wide; do not first-paint a pre-link (or
  // pre-unlink) GitopsView.
  const dropGitops = () => qc.removeQueries({ queryKey: ["gitops"] });

  async function unlink() {
    setBusy(true);
    try {
      await api.syncUnlink();
      setLinkFinished(false);
      setClosed(false);
      setStep(null);
      noteGitopsUnlinked();
      dropGitops();
      refresh();
    } finally {
      setBusy(false);
    }
  }

  return {
    wizard,
    initialStep: step ?? resume ?? undefined,
    readOnly,
    busy,
    openAt: (s: WizardStep) => setStep(s),
    onLinked: () => {
      dropGitops();
      refresh();
    },
    onDone: () => {
      setStep(null);
      setClosed(true);
      refresh();
    },
    unlink,
  };
}

/** The step the wizard opens at when nobody named one. */
export function resumeStep(status: SyncStatus): WizardStep {
  return resumeTarget(status) ?? 4;
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

/** A rail node's state (`Org Link.dc.html`): ✓ done · ◌ pending · ✕ refused · ? unknown. */
type NodeState = "done" | "pending" | "fail" | "unknown";
const GLYPH: Record<NodeState, { g: string; aria: string; cls: string }> = {
  done: { g: "✓", aria: "done", cls: "text-st-done" },
  pending: { g: "◌", aria: "pending", cls: "text-muted" },
  fail: { g: "✕", aria: "refused", cls: "text-st-blocked" },
  unknown: { g: "?", aria: "unknown", cls: "text-muted" },
};

/** `https://cloud.graphban.dev/link` → `cloud.graphban.dev/link`. */
function bare(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return bare(url) || "the console";
  }
}

/** "Expires in N minutes." from the console's `expires_at`; null when it sent none. */
export function expiryText(expiresAt: string | undefined, now = Date.now()): string | null {
  if (!expiresAt) return null;
  const t = new Date(expiresAt).getTime();
  if (Number.isNaN(t)) return null;
  const min = Math.max(0, Math.ceil((t - now) / 60000));
  if (min === 0) return "This code has expired.";
  return `Expires in ${min} ${min === 1 ? "minute" : "minutes"}.`;
}

/** "Couldn't reach <host>: <reason>." — the backend's reason, never a guess. */
export function unreachableText(url: string, reason: string | undefined | null): string {
  const r = (reason ?? "").trim().replace(/\.$/, "");
  return r ? `Couldn't reach ${hostOf(url)}: ${r}.` : `Couldn't reach ${hostOf(url)}.`;
}

const YOUR_STEP = (
  <span className="block font-mono text-[10px] uppercase tracking-wide text-accent">YOUR STEP</span>
);

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
  const [unreachable, setUnreachable] = React.useState<string | undefined>();
  const [org, setOrg] = React.useState(status.org);
  const [email, setEmail] = React.useState(status.linked_email ?? "");
  const [copied, setCopied] = React.useState("");
  const [err, setErr] = React.useState<string | null>(null);
  const [starting, setStarting] = React.useState(false);

  async function begin() {
    setErr(null);
    setStart(null);
    setSignIn("waiting");
    setUnreachable(undefined);
    setCopied("");
    setStep(2);
    setStarting(true);
    try {
      const r = await api.syncLinkStart(envUrl || url.trim());
      if (r.status === "started") setStart(r);
      else if (r.status === "refused") {
        setSignIn("refused");
        setRefusal(r.reason);
      } else {
        setUnreachable(r.reason);
        setSignIn("unreachable");
      }
    } catch (e) {
      setUnreachable(errorDetail(e, "the request failed"));
      setSignIn("unreachable");
    } finally {
      setStarting(false);
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
        } else if (r.status === "unknown") {
          setUnreachable(r.reason);
          setSignIn("unreachable");
        }
      } catch (e) {
        if (live) {
          setUnreachable(errorDetail(e, "the request failed"));
          setSignIn("unreachable");
        }
      }
    }, ms);
    return () => {
      live = false;
      window.clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, start, signIn]);

  // "Copied" is the button's own visible label for a moment, not a screen-reader-only note.
  const copyTimer = React.useRef<number | undefined>(undefined);
  React.useEffect(() => () => window.clearTimeout(copyTimer.current), []);
  function copy(label: string, text: string) {
    void navigator.clipboard?.writeText(text);
    setCopied(label);
    window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopied(""), 1500);
  }
  const copyLabel = (label: string, idle: string) => (copied === label ? "Copied" : idle);

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
  // Push later reveals the command; it never closes the wizard (design L3).
  const [later, setLater] = React.useState(false);

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
  /** The tag for cloud project `id`; `local` is the row whose stored tag is the fallback. */
  const nameOf = (id: string, local?: SyncProjectState) =>
    tagOf(
      cloud.projects.find((p) => p.id === id),
      local && local.cloud_project_id === id ? local.cloud_project_tag : null,
    );

  async function saveAndPush() {
    setSaving(true);
    setErr(null);
    try {
      await api.syncSaveProjectMap(
        Object.entries(choice)
          .filter(([, c]) => c)
          .map(([local_project_id, cloud_project_id]) => ({
            local_project_id,
            cloud_project_id,
            cloud_project_tag: cloud.projects.find((c) => c.id === cloud_project_id)?.tag ?? "",
          })),
      );
      // Choosing nothing at ③ is a finished link: reopening shows Linked, not ③.
      setLinkFinished(Object.values(choice).every((c) => !c));
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
  // ④'s rows are the projects chosen at ③ (initialised from the stored mapping).
  const toPush = status.projects.filter((p) => !!choice[p.project_id]);
  const nothingToPush = toPush.length === 0;
  const refused = pushes?.filter((p) => p.status === "refused").length ?? 0;
  const consoleUrl = envUrl || url.trim() || status.cloud_url || CONSOLE_URL;
  const orgLabel = org || status.org || "the org";
  const local = status.projects.length - synced.length;

  // Rows at ③ are labelled with the local project's tag, not its long name (which is the
  // label's tooltip). A failed projects read falls back to the name.
  const projectsQ = useProjects();
  const tagFor = (p: SyncProjectState) => projectsQ.data?.find((x) => x.id === p.project_id)?.tag || p.name;

  let line: Line;
  const cloudSeatRefused = step === 3 && cloud.state === "refused" && SEAT_REMOVED.includes(cloud.reason ?? "");
  if ((step <= 2 && signIn === "refused") || cloudSeatRefused) line = { text: "SIGN-IN · REFUSED", tone: "red" };
  else if (step <= 2 && signIn === "unreachable") line = { text: "CONSOLE · UNREACHABLE", tone: "grey" };
  else if (step === 1 || (step === 2 && signIn === "waiting" && !start && !starting))
    // No code exists yet: muted with a still dot (GRPH-1520's status-line list).
    line = { text: "SIGN-IN · NOT YET", tone: "muted" };
  else if (step === 2 && signIn === "waiting") line = { text: "SIGN-IN · WAITING", tone: "wait" };
  else if (step === 2) line = { text: `SIGNED IN · ${orgLabel.toUpperCase()}`, tone: "ok" };
  else if (step === 3 && cloud.state === "unknown") line = { text: "CONSOLE · UNREACHABLE", tone: "grey" };
  else if (step === 3)
    line = {
      text: `PROJECTS · ${synced.length} SYNCED · ${local} LOCAL`,
      // Green only once something syncs; nothing chosen yet is muted, not done.
      tone: synced.length > 0 ? "ok" : "muted",
    };
  else if (nothingToPush) line = { text: "PUSH · NOTHING TO PUSH", tone: "muted" };
  else if (pushing) line = { text: "PUSH · PUSHING", tone: "wait" };
  else if (seatRemoved) line = { text: "SIGN-IN · SEAT REMOVED", tone: "red" };
  else if (!pushes) line = { text: "PUSH · NOT YET", tone: "muted" };
  else if (refused) line = { text: `PUSH · ${done} DONE · ${refused} REFUSED`, tone: "red" };
  else if (done === 0) line = { text: "CONSOLE · UNREACHABLE", tone: "grey" };
  // Green only once something is done.
  else line = { text: `PUSH · ${done} DONE · 0 REFUSED`, tone: "ok" };
  const pulsing = line.tone === "wait";

  // The rail (Org Link.dc.html): a glyph and a sub-line per node.
  const signedInNow = signIn === "approved";
  const signInDone = signedInNow || step > 2;
  const who = [org || status.org, email].filter(Boolean).join(" · ");
  const totalNodes = (pushes ?? []).reduce((n, p) => n + (p.status === "done" ? p.nodes ?? 0 : 0), 0);
  const nodes: [NodeState, string][] = [
    [step > 1 ? "done" : "pending", bare(consoleUrl)],
    step < 2
      ? ["pending", "your step"]
      : signInDone
        ? ["done", who || "signed in"]
        : signIn === "refused"
          ? ["fail", "refused"]
          : signIn === "unreachable"
            ? ["unknown", "couldn't reach"]
            : ["pending", start ? "waiting…" : "your step"],
    step < 3
      ? ["pending", "not yet"]
      : step > 3
        ? ["done", `${synced.length} synced · ${local} local`]
        : cloud.state === "unknown"
          ? ["unknown", "couldn't reach"]
          : cloud.state === "refused"
            ? ["fail", "refused"]
            : ["pending", `${synced.length} synced · ${local} local`],
    step < 4
      ? ["pending", "not yet"]
      : nothingToPush
        ? ["done", "nothing to push"]
        : pushing
          ? ["pending", "pushing…"]
          : seatRemoved
            ? ["fail", "seat removed"]
            : refused
              ? ["fail", `refused · ${refused}`]
              : pushes && done === 0
                ? ["unknown", "couldn't reach"]
                : done
                  ? ["done", `${totalNodes.toLocaleString("en-US")} nodes`]
                  : ["pending", "not yet"],
  ];

  // ← Back / Next → (bottom right). A disabled Next always says why.
  let nextLabel = "Next →";
  let nextOff = false;
  let nextWhy = "";
  let onNext: () => void = () => {};
  if (step === 1) {
    nextOff = readOnly || !(envUrl || url.trim()) || starting;
    nextWhy = !(envUrl || url.trim()) ? "Enter the console's URL." : "";
    onNext = begin;
  } else if (step === 2) {
    nextOff = !signedInNow;
    nextWhy = nextOff
      ? signIn === "waiting" && start
        ? "Approve the code in the console first."
        : signIn === "waiting"
          ? "Get a sign-in code first."
          : "Sign in first."
      : "";
    onNext = () => setStep(3);
  } else if (step === 3) {
    nextOff = readOnly || saving || cloud.state !== "ok";
    nextWhy = cloud.state === "loading"
        ? "Reading projects…"
        : cloud.state !== "ok"
          ? "Projects couldn't be loaded."
          : "";
    onNext = () => void saveAndPush();
  } else if (nothingToPush || done > 0) {
    nextLabel = "Done";
    onNext = () => onDone?.();
  } else if (seatRemoved) {
    nextLabel = "Sign in again";
    nextOff = readOnly;
    onNext = begin;
  } else {
    nextLabel = pushing ? "Pushing…" : pushes ? "Push again" : "Push now";
    nextOff = readOnly || pushing;
    onNext = () => void push();
  }

  const cmdLink = `graphban link --cloud-url ${consoleUrl}`;

  return (
    <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4" data-testid="link-wizard">
      <ol className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label="Link steps">
        {RAIL.map((label, i) => {
          const [state, sub] = nodes[i];
          const gl = GLYPH[state];
          const cur = step === i + 1;
          return (
            <li
              key={label}
              data-rail-node={i + 1}
              data-state={state}
              aria-current={cur ? "step" : undefined}
              className={cn(
                "flex min-w-0 flex-col gap-0.5 rounded-[8px] border px-2.5 py-1.5",
                cur ? "border-accent/70 bg-accent/5" : state === "fail" ? "border-st-blocked/45" : "border-line-2",
              )}
            >
              <span className="flex items-center gap-1.5 font-mono text-small">
                <span className={cur ? "text-accent" : "text-faint"}>{["①", "②", "③", "④"][i]}</span>
                <span className={cur ? "text-accent" : "text-fg"}>{label}</span>
                <span aria-label={gl.aria} className={cn("ml-auto", gl.cls)}>
                  {gl.g}
                </span>
              </span>
              <span data-rail-sub className={cn("truncate font-mono text-[10px]", state === "unknown" ? "text-muted" : "text-faint")}>
                {sub}
              </span>
            </li>
          );
        })}
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
        <p id="link-readonly" className="mb-3 text-small text-faint">
          Only a box admin can link this deployment. You can see where it stands here.
        </p>
      )}

      {step === 1 && (
        <div className="space-y-1.5">
          <label className="block text-small text-muted" htmlFor="console-url">
            Console URL
          </label>
          <Input
            id="console-url"
            value={envUrl || url}
            readOnly={!!envUrl || readOnly}
            aria-describedby="console-url-help"
            onChange={(e) => setUrl(e.target.value)}
          />
          {envUrl ? (
            <p id="console-url-help" className="flex flex-wrap items-baseline gap-2 text-small text-faint">
              <span className="font-mono text-[10px] uppercase tracking-wide text-muted">SOURCE · env</span>
              <span>
                Set by this deployment's environment. Change{" "}
                <code className="font-mono text-[10.5px]">SYNC_CLOUD_URL</code> to move it.
              </span>
            </p>
          ) : (
            <p id="console-url-help" className="text-small text-faint">
              The address of your org's Graphban console.
            </p>
          )}
        </div>
      )}

      {step === 2 && (
        <div className="space-y-3">
          {!start && signIn === "waiting" && !signedInNow && (
            <Button size="sm" onClick={begin} disabled={readOnly || starting}>
              {starting ? "Getting a code…" : "Get a sign-in code"}
            </Button>
          )}
          {start && signIn === "waiting" && (
            <div className="space-y-2" data-testid="code-block">
              {YOUR_STEP}
              <div className="flex items-center gap-3">
                <span className="font-mono text-[22px] tracking-[0.2em]" data-testid="user-code">
                  {start.user_code}
                </span>
                <Button size="sm" variant="ghost" onClick={() => copy("code", start.user_code ?? "")}>
                  {copyLabel("code", "Copy")}
                </Button>
              </div>
              <p className="text-small text-muted">
                Open <strong className="font-mono font-semibold text-fg">{bare(start.verification_url ?? "")}</strong> and
                enter the code.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <a
                  className="inline-flex h-8 items-center rounded-[8px] border border-control px-3 text-[12px]"
                  href={start.verification_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open the console →
                </a>
                <Button size="sm" variant="ghost" onClick={() => copy("link", start.verification_url ?? "")}>
                  {copyLabel("link", "Copy link")}
                </Button>
                {expiryText(start.expires_at) && (
                  <span className="text-small text-faint" data-testid="expiry">
                    {expiryText(start.expires_at)}
                  </span>
                )}
              </div>
              <p className="text-small text-muted">Waiting for you to approve it in the console.</p>
            </div>
          )}
          {signedInNow && (
            <p className="text-body text-st-done">
              ✓ Signed in to {orgLabel}
              {email ? " as " : ""}
              {email ? <code className="font-mono text-[12px]">{email}</code> : null}.
            </p>
          )}
          {signIn === "refused" && (
            <>
              <p className="flex gap-1.5 text-body text-st-blocked">
                <span aria-hidden>✕</span>
                <span className="text-st-blocked">{refusalText(refusal, orgLabel)}</span>
              </p>
              <Button size="sm" onClick={begin}>
                Get a new code
              </Button>
            </>
          )}
          {signIn === "unreachable" && (
            <>
              <p className="text-body text-faint" data-testid="unreachable">
                ? {unreachableText(consoleUrl, unreachable)}
              </p>
              <Button size="sm" onClick={begin}>
                Try again
              </Button>
            </>
          )}
          {!signedInNow && (
            <div className="space-y-1" data-testid="prefer-terminal">
              <span className="block font-mono text-[10px] uppercase tracking-wide text-faint">PREFER THE TERMINAL?</span>
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded-[6px] border border-line-2 bg-surface-3 px-2 py-1 font-mono text-small">
                  <span aria-hidden className="text-faint">$ </span>
                  {cmdLink}
                </code>
                <Button size="sm" variant="ghost" onClick={() => copy("cmd", cmdLink)}>
                  {copyLabel("cmd", "Copy")}
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      {step === 3 && (
        <div className="space-y-3">
          {YOUR_STEP}
          {cloud.state === "loading" && <p className="text-body text-faint">Reading projects…</p>}
          {cloud.state === "unknown" && (
            <>
              <p className="text-body text-faint" data-testid="unreachable">
                ? {unreachableText(consoleUrl, cloud.reason)}
              </p>
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
              ◌ You don't have access to any projects in {orgLabel} yet. Ask an org admin.
            </p>
          )}
          {cloud.state === "ok" && cloud.projects.length > 0 && (
            <>
              <p className="text-body text-muted">
                Pick where each local project syncs in {orgLabel}. Local only keeps it on this box.
              </p>
              <div role="list" aria-label="Local projects" className="space-y-2">
                {status.projects.map((p: SyncProjectState) => {
                  const value = choice[p.project_id] ?? LOCAL;
                  const id = `link-proj-${p.project_id}`;
                  return (
                    <div key={p.project_id} role="listitem" className="flex items-center gap-3">
                      <label htmlFor={id} className="min-w-0 flex-1 truncate font-mono text-body" title={p.name}>
                        {tagFor(p)}
                      </label>
                      <select
                        id={id}
                        aria-label={`Cloud project for ${p.name}`}
                        value={value}
                        disabled={readOnly}
                        onChange={(e) => setChoice({ ...choice, [p.project_id]: e.target.value })}
                        className="h-8 rounded-[8px] border border-control bg-surface-2 px-2 text-[12px]"
                      >
                        <option value={LOCAL}>Local only</option>
                        {cloud.projects.map((c) => (
                          <option key={c.id} value={c.id}>
                            {tagOf(c)}
                          </option>
                        ))}
                      </select>
                      <span className="w-24 font-mono text-[10px] text-faint">
                        {value ? `☁ ${nameOf(value, p)}` : "local only"}
                      </span>
                    </div>
                  );
                })}
              </div>
              {synced.length === 0 && (
                <p className="text-small text-faint">
                  Nothing will sync. The box stays linked to {orgLabel}.
                </p>
              )}
            </>
          )}
          {err && <p className="text-small text-st-blocked">{err}</p>}
        </div>
      )}

      {step === 4 && (
        <div className="space-y-3">
          {seatRemoved && (
            <p role="alert" className="text-body text-st-blocked">
              ✕ Your seat in {orgLabel} was removed. Nothing is syncing.
            </p>
          )}
          {nothingToPush && (
            <p className="text-body text-muted">Nothing to push. Every project is local only.</p>
          )}
          {toPush.map((p) => {
            const r = pushing ? undefined : pushes?.find((x) => x.project_id === p.project_id);
            const tag = nameOf(choice[p.project_id] ?? p.cloud_project_id ?? "", p);
            return (
              <div key={p.project_id} data-push-row className="flex items-baseline gap-3 text-body">
                <span className="w-32 truncate">{p.name}</span>
                {pushing && <span className="font-mono text-[10px] uppercase text-muted">◌ PUSHING…</span>}
                {!pushing && !r && <span className="text-faint">☁ {tag}</span>}
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
                  <span className="text-faint">? UNKNOWN — {unreachableText(consoleUrl, r.reason)}</span>
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
          {!nothingToPush && done === 0 && !seatRemoved && !pushes && !pushing && (
            <button
              type="button"
              className="text-small text-accent underline-offset-2 hover:underline"
              aria-expanded={later}
              onClick={() => setLater(true)}
            >
              Push later
            </button>
          )}
          {later && !nothingToPush && done === 0 && !seatRemoved && (
            <div className="space-y-2" data-testid="push-later">
              <p className="text-small text-faint">
                ④ stays pending. Run this on the box when you're ready.
              </p>
              <div className="flex items-center gap-2">
                <code className="rounded-[6px] border border-line-2 bg-surface-3 px-2 py-1 font-mono text-small">
                  graphban sync
                </code>
                <Button size="sm" variant="ghost" onClick={() => copy("command", "graphban sync")}>
                  {copyLabel("command", "Copy")}
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center justify-end gap-2 border-t border-line pt-3" data-testid="wizard-nav">
        {nextOff && nextWhy && !readOnly && (
          <span id="link-next-why" className="mr-auto text-small text-faint">
            {nextWhy}
          </span>
        )}
        {step === 4 && done > 0 && (
          <Button size="sm" variant="ghost" onClick={() => void push()} disabled={readOnly || pushing}>
            {pushing ? "Pushing…" : "Push again"}
          </Button>
        )}
        {step > 1 && (
          <Button size="sm" variant="ghost" onClick={() => setStep((step - 1) as WizardStep)} disabled={pushing || saving}>
            ← Back
          </Button>
        )}
        <Button
          size="sm"
          onClick={onNext}
          disabled={nextOff}
          // Read-only: the admin note above is the reason; otherwise the line beside Next.
          aria-describedby={nextOff ? (readOnly ? "link-readonly" : nextWhy ? "link-next-why" : undefined) : undefined}
        >
          {nextLabel}
        </Button>
      </div>
    </div>
  );
}
