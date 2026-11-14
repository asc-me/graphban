import { cloudTagText, UNKNOWN_CLOUD_TAG, UNKNOWN_TAG_TOOLTIP } from "@/components/shell/CloudTag";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { useProjects } from "@/lib/queries";
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

/** The "Linked as" summary (Org Link design 2e): shown once ④ is done, and when a finished
 *  link is reopened. Open org overview leads on; the refused variants make Sign in again primary. */
export function LinkedSummary({
  status,
  deploymentName,
  readOnly = false,
  busy = false,
  onOpenOverview,
  onChooseProjects,
  onSignIn,
}: {
  status: SyncStatus;
  /** This deployment's name in the org (the `here` row of /api/sync/org), when it was read. */
  deploymentName?: string | null;
  readOnly?: boolean;
  busy?: boolean;
  onOpenOverview: () => void;
  onChooseProjects: () => void;
  onSignIn: () => void;
}) {
  const org = status.org || "the org";
  const refused = status.state === "refused";
  const seatRemoved = refused && !status.legacy;
  const keyRefused = refused && !!status.legacy;
  const mapped = status.projects.filter((p) => p.mapped);
  const local = status.projects.length - mapped.length;
  // SyncProjectState carries no local tag: read it from the projects list the client already has.
  const { data: projects } = useProjects();
  const localTag = (id: string, name: string) => projects?.find((x) => x.id === id)?.tag || name;

  return (
    <div className="space-y-3" data-testid="linked-summary">
      {seatRemoved && (
        <p role="alert" className="rounded-md border border-[rgba(255,107,107,0.35)] bg-[rgba(255,107,107,0.06)] px-3 py-2 text-body font-semibold text-fg">
          <span aria-hidden className="text-st-blocked">✕ </span>Your seat in {org} was removed. Nothing is syncing.
        </p>
      )}
      {keyRefused && (
        <p role="alert" className="rounded-md border border-[rgba(255,107,107,0.35)] bg-[rgba(255,107,107,0.06)] px-3 py-2 text-body font-semibold text-fg">
          <span aria-hidden className="text-st-blocked">✕ </span>This link used a key. Sign in to keep syncing.
        </p>
      )}
      <h3 className="text-[15px] font-semibold">
        <span aria-hidden className="text-st-done">⛓ </span>
        {deploymentName ? (
          <>
            Linked as <span className="font-mono">{deploymentName}</span>
          </>
        ) : (
          "Linked"
        )}
      </h3>
      <p className="text-body">
        This deployment is linked to <strong className="font-semibold text-fg">{org}</strong>
        {status.linked_email ? (
          <>
            {" as "}
            <code className="font-mono text-[12px] text-fg">{status.linked_email}</code>
          </>
        ) : null}
        .
      </p>
      <span className="font-mono text-meta text-faint">SYNCED PROJECTS</span>
      <ul className="max-w-[520px] space-y-1.5">
        {mapped.map((p) => {
          const result = seatRemoved ? "refused" : p.last_push_result;
          return (
            <li
              key={p.project_id}
              data-synced-project
              className="flex items-center justify-between gap-2 rounded-md border border-line bg-surface px-3 py-2 text-body"
            >
              {/* Local tag → cloud tag in every state, the seat-removed one included (Org Link vDone). */}
              <span className="flex min-w-0 items-baseline gap-2 font-mono" data-row-tags>
                <span className="max-w-40 truncate uppercase">{localTag(p.project_id, p.name)}</span>
                <span aria-hidden className="text-faint">→</span>
                <span
                  className="uppercase text-fg-2"
                  title={cloudTagText(p.cloud_project_tag) === UNKNOWN_CLOUD_TAG ? UNKNOWN_TAG_TOOLTIP : undefined}
                >
                  {cloudTagText(p.cloud_project_tag)}
                </span>
              </span>
              {result === "ok" && p.last_push_at && (
                <span className="font-mono text-[10px] font-semibold uppercase text-st-done">☁ SYNCED {clockTime(p.last_push_at)}</span>
              )}
              {result === "refused" && <span className="font-mono text-[10px] font-semibold uppercase text-st-blocked">☁ REFUSED</span>}
              {result === "unknown" && <span className="font-mono text-[10px] font-semibold uppercase text-faint">☁ UNKNOWN</span>}
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
      <div className="flex flex-wrap items-center gap-2 pt-1">
        {refused && (
          <Button size="sm" onClick={onSignIn} disabled={readOnly || busy}>
            Sign in again
          </Button>
        )}
        <Button size="sm" variant={refused ? "outline" : "default"} onClick={onOpenOverview}>
          Open org overview
        </Button>
        <Button size="sm" variant="outline" onClick={onChooseProjects} disabled={readOnly || busy}>
          Choose projects
        </Button>
        {!refused && (
          <Button size="sm" variant="outline" onClick={onSignIn} disabled={readOnly || busy}>
            Sign in again
          </Button>
        )}
      </div>
    </div>
  );
}
