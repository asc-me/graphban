import { cloudTagText, UNKNOWN_CLOUD_TAG, UNKNOWN_TAG_TOOLTIP } from "@/components/shell/CloudTag";
import * as React from "react";

import { Button } from "@/components/ui/button";
import type { SyncStatus } from "@/lib/types";

/** The Unlink line, shared by Settings → Sync / Link and the Organization window. */
export const UNLINK_COPY = "Stops all sync. Every project becomes local only. Cloud items remain.";

/** Zone-less timestamps are UTC (SQLite hands them back naive). `14:02`. */
export function clockTime(iso: string): string {
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(zoned ? iso : `${iso}Z`).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** The Linked panel (GRPH-P67 §5, L5/L6): who the box is signed in as, what syncs, and the
 *  refusals. Unknown is grey, never a refusal; the old-key relink warning is muted. */
export function LinkedPanel({
  status,
  orgHref,
  readOnly = false,
  unlinkable = true,
  busy = false,
  onChooseProjects,
  onSignIn,
  onUnlink,
  confirmUnlink = false,
}: {
  status: SyncStatus;
  orgHref?: string;
  readOnly?: boolean;
  /** False when the link is baked in by env vars: Unlink cannot clear it, but signing in
   *  again and choosing projects still work (GRPH-1520). */
  unlinkable?: boolean;
  busy?: boolean;
  onChooseProjects: () => void;
  onSignIn: () => void;
  onUnlink: () => void;
  /** The Organization window asks before it unlinks; Settings unlinks on the click. */
  confirmUnlink?: boolean;
}) {
  const [confirming, setConfirming] = React.useState(false);
  const org = status.org || "the org";
  const legacy = !!status.legacy;
  const refused = status.state === "refused";
  const seatRemoved = refused && !legacy;
  const keyRefused = refused && legacy;
  const mapped = status.projects.filter((p) => p.mapped);
  const local = status.projects.length - mapped.length;

  return (
    <div className="space-y-3" data-testid="linked-panel">
      <p className="text-body" data-linked-as>
        This deployment is linked to <strong className="font-semibold text-fg">{org}</strong>
        {status.linked_email ? (
          <>
            {" as "}
            <code className="font-mono text-[12px] text-fg">{status.linked_email}</code>
          </>
        ) : null}
        .
      </p>

      {seatRemoved && (
        <p className="text-body text-st-blocked">Your seat in {org} was removed. Nothing is syncing.</p>
      )}
      {keyRefused && (
        <p className="text-body text-st-blocked">This link used a key. Sign in to keep syncing.</p>
      )}
      {legacy && !refused && (
        <p className="text-small text-faint" data-testid="relink-warning">
          This deployment still uses an old link key. Sign in again to link it to your seat. It
          keeps syncing until then.
        </p>
      )}

      <ul className="space-y-1">
        {mapped.map((p) => {
          const result = seatRemoved ? "refused" : p.last_push_result;
          return (
            <li key={p.project_id} data-synced-project className="flex items-baseline gap-2 text-body">
              <span className="max-w-40 truncate font-mono">{p.name}</span>
              {!seatRemoved && <span aria-hidden className="text-faint">→</span>}
              {/* Seat removed: the row reads only ☁ REFUSED (Org Link L3, linked seat removed). */}
              {!seatRemoved && (
                <span
                  className="font-mono text-[10px] uppercase text-faint"
                  title={cloudTagText(p.cloud_project_tag) === UNKNOWN_CLOUD_TAG ? UNKNOWN_TAG_TOOLTIP : undefined}
                >
                  ☁ {cloudTagText(p.cloud_project_tag)}
                </span>
              )}
              {result === "ok" && p.last_push_at && (
                <span className="font-mono text-[10px] uppercase text-st-done">· ☁ SYNCED {clockTime(p.last_push_at)}</span>
              )}
              {result === "refused" && (
                <span className="font-mono text-[10px] uppercase text-st-blocked">☁ REFUSED</span>
              )}
              {result === "unknown" && (
                <span className="font-mono text-[10px] uppercase text-faint">☁ UNKNOWN</span>
              )}
            </li>
          );
        })}
      </ul>
      <p className="text-small text-faint">
        {local === 1 ? "1 project is local only." : `${local} projects are local only.`}
      </p>

      {seatRemoved && (
        <p className="text-small text-faint">
          Anyone with a seat in {org} can sign this box in. Your project choices are kept and
          checked again.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <Button size="sm" variant="ghost" onClick={onChooseProjects} disabled={readOnly || busy}>
          Choose projects
        </Button>
        <Button
          size="sm"
          variant={legacy || refused ? "default" : "ghost"}
          onClick={onSignIn}
          disabled={readOnly || busy}
        >
          Sign in again
        </Button>
        {orgHref && (
          <a className="text-small text-accent" href={orgHref} target="_blank" rel="noreferrer">
            Open the org →
          </a>
        )}
        <Button
          size="sm"
          variant="danger"
          onClick={confirmUnlink ? () => setConfirming(true) : onUnlink}
          disabled={readOnly || busy || !unlinkable || confirming}
        >
          Unlink
        </Button>
      </div>
      {confirming ? (
        <div role="alertdialog" aria-label="Unlink this deployment" className="flex flex-wrap items-center gap-2.5">
          <span className="min-w-0 flex-1 text-body text-fg-2">{UNLINK_COPY}</span>
          <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
          <Button
            size="sm"
            variant="danger"
            disabled={busy}
            onClick={() => {
              setConfirming(false);
              onUnlink();
            }}
          >
            {busy ? "Unlinking…" : "Unlink this deployment"}
          </Button>
        </div>
      ) : (
        <p className="text-small text-faint">{UNLINK_COPY}</p>
      )}
    </div>
  );
}
