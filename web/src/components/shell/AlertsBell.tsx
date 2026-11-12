import { Bell } from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { useConfig, useMyAlerts, useProjects, usePushStatus, usePushSubscribe, useReadAlert } from "@/lib/queries";
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
  const failed = q.isError && !q.data;
  const unread = q.data?.unread ?? 0;
  const label = failed ? "!" : q.data ? String(unread) : "…";

  return (
    <div className="relative">
      <button
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
      {open && (
        <div
          role="dialog"
          aria-label="Alerts"
          className="absolute right-0 top-11 z-50 max-h-[420px] w-[360px] overflow-auto rounded-lg border border-line bg-surface p-2 shadow-lg"
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
              <div className={`text-[13px] ${a.read ? "text-fg-2" : "font-semibold"}`}>{a.question}</div>
              <div className="mt-0.5 font-mono text-[10.5px] text-faint">
                {[a.ticket, a.prd, a.asked_by && `asked by ${a.asked_by}`, a.created_at && new Date(a.created_at).toLocaleString()]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
            </Link>
          ))}
          <BrowserPush />
        </div>
      )}
    </div>
  );
}
