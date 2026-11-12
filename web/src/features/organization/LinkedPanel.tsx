import { Button } from "@/components/ui/button";
import type { SyncStatus } from "@/lib/types";

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
}) {
  const org = status.org || "the org";
  const legacy = !!status.legacy;
  const refused = status.state === "refused";
  const seatRemoved = refused && !legacy;
  const keyRefused = refused && legacy;
  const mapped = status.projects.filter((p) => p.mapped);
  const local = status.projects.length - mapped.length;

  return (
    <div className="space-y-3" data-testid="linked-panel">
      <p className="text-body">
        This deployment is linked to {org}
        {status.linked_email ? ` as ${status.linked_email}` : ""}.
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
            <li key={p.project_id} className="flex items-baseline gap-3 text-body">
              <span className="w-40 truncate">{p.name}</span>
              <span className="font-mono text-[10px] uppercase text-faint">
                ☁ {(p.cloud_project_id ?? "").toUpperCase()}
              </span>
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
        <Button size="sm" variant="danger" onClick={onUnlink} disabled={readOnly || busy || !unlinkable}>
          Unlink
        </Button>
      </div>
      <p className="text-small text-faint">
        Stops all sync. Every project becomes local only. Cloud items remain.
      </p>
    </div>
  );
}
