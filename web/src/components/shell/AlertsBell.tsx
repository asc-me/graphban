import { Bell } from "lucide-react";
import * as React from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";

import { useConfig, useMyAlerts, useProjects, usePushStatus, usePushSubscribe, useReadAlert } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import { projectPath, rememberProjectTag } from "@/lib/routes";
import type { AlertRow } from "@/lib/types";

function b64ToBytes(b64: string): Uint8Array {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

const pushSupported = () =>
  typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

/**
 * "Notify me in this browser" (GRPH-1528). Permission is asked ONLY on the click, never on
 * load. No VAPID keys: says so and offers no button. Denied / unsupported are shown as such.
 */
function BrowserPush() {
  const status = usePushStatus();
  const subscribe = usePushSubscribe();
  const [done, setDone] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  if (!status.data) return null;
  if (!status.data.configured || !status.data.public_key) {
    return <div className="border-t border-line p-3 text-[12px] text-muted">Browser notifications are not set up on this deployment.</div>;
  }
  // Service workers (so Web Push) exist only in a secure context: HTTPS or localhost.
  if (typeof window !== "undefined" && window.isSecureContext === false) {
    return (
      <div className="border-t border-line p-3 text-[12px] text-muted">
        Browser notifications need this site on HTTPS. In-app alerts still work.{" "}
        <a className="underline" href="https://github.com/graphban/graphban/blob/main/docs/deploy.md#browser-push-optional" target="_blank" rel="noreferrer">
          Why?
        </a>
      </div>
    );
  }
  if (!pushSupported()) {
    return <div className="border-t border-line p-3 text-[12px] text-muted">This browser does not support push notifications.</div>;
  }
  if (Notification.permission === "denied") {
    return <div className="border-t border-line p-3 text-[12px] text-muted">Notifications are blocked for this site in your browser settings.</div>;
  }
  if (done) return <div className="border-t border-line p-3 text-[12px] text-muted">Notifications are on in this browser.</div>;
  const enable = async () => {
    setError(null);
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        setError(perm === "denied" ? "Notifications are blocked for this site in your browser settings." : "Permission was not granted.");
        return;
      }
      await navigator.serviceWorker.register("/sw.js");
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: b64ToBytes(status.data!.public_key!) as BufferSource,
      });
      const j = sub.toJSON();
      await subscribe.mutateAsync({ endpoint: sub.endpoint, keys: { p256dh: j.keys?.p256dh ?? "", auth: j.keys?.auth ?? "" } });
      setDone(true);
    } catch {
      setError("Could not turn on notifications in this browser.");
    }
  };
  return (
    <div className="border-t border-line p-3">
      <button type="button" onClick={enable} className="rounded-md border border-control px-2.5 py-1 text-[12px] hover:border-control-hover">
        Notify me in this browser
      </button>
      {error && (
        <div role="alert" className="mt-1.5 text-[12px] text-st-blocked">
          {error}
        </div>
      )}
    </div>
  );
}

function impactLabel(a: AlertRow): string {
  if (a.impact === "drain_waiting") return "Drain waiting";
  if (a.impact === "blocks" && a.ticket) return `Blocks ${a.ticket}`;
  return "Question";
}

/**
 * The bell (GRPH-1527, P75 D4): unread alert count and a panel, newest first. A failed read
 * shows "could not load alerts" — never 0, which would tell a person nothing is waiting on them.
 */
export function AlertsBell() {
  const q = useMyAlerts();
  const { data: projects = [] } = useProjects();
  const { data: config } = useConfig();
  const hosted = config?.hosted_mode ?? false;
  // The Triage row lives in the ALERT's project, not whichever one is open: hosted links by
  // tag; self-host's flat route resolves the last-used project, so that is set on click.
  const tagOf = (a: AlertRow) => projects.find((p) => p.id === a.project)?.tag ?? null;
  const hrefOf = (a: AlertRow) => {
    const tag = tagOf(a);
    const base = hosted && tag ? projectPath(tag, "triage") : "/triage";
    return `${base}?item=${encodeURIComponent(a.ticket ?? a.wait)}`;
  };
  const read = useReadAlert();
  const [open, setOpen] = React.useState(false);
  const btnRef = React.useRef<HTMLButtonElement>(null);
  // The panel is portaled to <body> with fixed placement (GRPH-1559): inside the top bar its
  // z-50 is trapped in the header's stacking context, below the page content.
  const [pos, setPos] = React.useState<{ top: number; right: number } | null>(null);
  React.useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const r = btnRef.current?.getBoundingClientRect();
      if (r) setPos({ top: r.bottom + 8, right: Math.max(8, window.innerWidth - r.right) });
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open]);
  const failed = q.isError && !q.data;
  const unread = q.data?.unread ?? 0;
  const label = failed ? "!" : q.data ? String(unread) : "…";

  return (
    <div className="relative">
      <button
        ref={btnRef}
        type="button"
        aria-label={failed ? "Alerts: could not load alerts" : q.data ? `Alerts, ${unread} unread` : "Alerts, loading"}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex h-9 items-center gap-1.5 rounded-lg border border-control bg-surface px-2.5 text-muted transition-colors hover:border-control-hover"
      >
        <Bell size={14} />
        <span data-testid="alerts-count" className="font-mono text-small">
          {label}
        </span>
      </button>
      {open && createPortal(
        <div
          role="dialog"
          aria-label="Alerts"
          style={{ top: pos?.top ?? 56, right: pos?.right ?? 8 }}
          className="fixed z-50 max-h-[420px] w-[360px] overflow-auto rounded-lg border border-line bg-surface p-2 shadow-lg"
        >
          {failed && (
            <div role="alert" className="p-3 text-[13px] text-st-blocked">
              could not load alerts
              <button type="button" className="ml-2 underline" onClick={() => q.refetch()}>
                Retry
              </button>
            </div>
          )}
          {q.data && q.data.alerts.length === 0 && (
            <div className="p-3 text-[13px] text-muted">Nothing needs you.</div>
          )}
          {q.data?.alerts.map((a) => (
            <Link
              key={a.id}
              to={hrefOf(a)}
              onClick={() => {
                const tag = tagOf(a);
                if (tag) rememberProjectTag(tag);
                if (!a.read) read.mutate(a.id);
                setOpen(false);
              }}
              className="block rounded-md p-2.5 hover:bg-surface-2"
            >
              <div className="flex items-center gap-1.5">
                <span
                  data-testid="alert-impact"
                  className="flex-none rounded-md border border-st-blocked/40 bg-st-blocked/[0.08] px-1.5 py-0.5 font-mono text-[10px] text-st-blocked"
                >
                  {impactLabel(a)}
                </span>
                {!a.read && <span aria-label="unread" className="h-1.5 w-1.5 flex-none rounded-full bg-fg" />}
                <div
                  data-testid="alert-headline"
                  className={`min-w-0 flex-1 truncate text-[13px] ${a.read ? "text-fg-2" : "font-semibold text-fg"}`}
                >
                  {a.title || a.question}
                </div>
              </div>
              {a.title && a.title !== a.question && (
                <div className="mt-0.5 line-clamp-2 text-[12px] text-muted">{a.question}</div>
              )}
              <div className="mt-0.5 font-mono text-[10.5px] text-faint">
                {[a.project_tag, a.created_at && <span key="age" title={new Date(a.created_at).toLocaleString()}>{relTime(a.created_at)}</span>, a.prd, a.asked_by && `asked by ${a.asked_by}`]
                  .filter(Boolean)
                  .flatMap((x, i) => (i === 0 ? [x] : [" · ", x]))}
              </div>
            </Link>
          ))}
          <BrowserPush />
        </div>,
        document.body,
      )}
    </div>
  );
}
