import { ChevronDown, ChevronUp, Lock } from "lucide-react";

import { cn } from "@/lib/cn";
import { useSaveHarnessLadder, useHarnessLadder } from "@/lib/queries";
import type { HarnessLadderRung } from "@/lib/types";

/** The 4xx envelope is `{detail}`; `request()` throws with the raw body as message. Shown verbatim. */
export function refusalText(err: unknown): string {
  const raw = err && typeof err === "object" && "message" in err ? String((err as { message: unknown }).message) : "";
  try {
    const p = JSON.parse(raw);
    if (p && typeof p.detail === "string") return p.detail;
  } catch {
    /* not JSON */
  }
  return raw || "The ladder could not be saved.";
}

const Num = ({ n }: { n: string }) => <span className="text-center font-mono text-[12px] text-muted">{n}</span>;
const btn = "inline-flex h-7 items-center justify-center rounded-[7px] border border-control px-[9px] text-[12px] text-fg-2 hover:bg-surface-3 disabled:text-line-3 disabled:hover:bg-transparent";
const tile = "grid items-center gap-2.5 rounded-[10px] px-3 py-2.5";
const liveTile = "border border-line bg-surface-2";
const gone = "border-[1.5px] border-dashed border-line";
const Tag = ({ children }: { children: React.ReactNode }) => (
  <span className="font-mono text-meta font-semibold tracking-[0.6px] text-fg-2">{children}</span>
);
const Rung = ({ label, description, struck }: { label: string; description?: string; struck?: boolean }) => (
  <>
    <span className={cn("font-mono text-[13px] text-fg", struck && "line-through decoration-faint")}>{label}</span>
    {description && <span className={cn("text-[12px] text-muted", struck && "line-through decoration-faint")}>{description}</span>}
  </>
);

/**
 * The Capacity tab's fallback ladder: the project policy (read-only) beside the caller's own
 * override, headed with the person's name (`<name>'s override`). A rung the policy removed is never offered back: it reads "Can't add back" with no
 * control. A refusal on save is shown as the server said it, and nothing changes locally — the
 * rungs drawn are always the server's last answer.
 */
export function Ladder({ projectId, name }: { projectId?: string; name?: string }) {
  const { data, isError } = useHarnessLadder(projectId);
  const save = useSaveHarnessLadder(projectId);
  if (isError || !data) return null;

  const person = name?.trim() || "";
  const heading = person ? `${person}'s override` : "Your override";
  const claims = person ? `work ${person} claims` : "work you claim";
  const allowed = data.policy.filter((r) => !r.removed);
  const byRung = new Map<string, HarnessLadderRung>(data.policy.map((r) => [r.rung, r]));
  // A rung the policy removed — marked `removed`, or listed in `refused` because a saved profile
  // still carries it — is never live in the override: `effective` drops it, and a write must not
  // send it back. It is drawn only as a "Can't add back" row.
  const policyRemoved = [
    ...data.policy.filter((r) => r.removed),
    ...(data.refused ?? []).filter((r) => !data.policy.some((p) => p.rung === r.rung && p.removed)),
  ].filter((r, i, a) => a.findIndex((x) => x.rung === r.rung) === i);
  const blocked = new Set(policyRemoved.map((r) => r.rung));
  const live = (data.override ?? data.effective)
    .map((r) => r.rung)
    .filter((rung) => !blocked.has(rung) && byRung.has(rung));
  const userRemoved = data.override ? allowed.filter((r) => !live.includes(r.rung)) : [];
  const write = (next: string[] | null) => save.mutate(next);
  const move = (i: number, d: number) => {
    const o = live.slice(); const j = i + d;
    if (j < 0 || j >= o.length) return;
    [o[i], o[j]] = [o[j], o[i]];
    write(o);
  };

  return (
    <div data-testid="capacity-ladder" className="flex flex-col gap-3.5">
      <div className="flex flex-col gap-1">
        <h2 className="text-[16px] font-semibold tracking-[-0.01em]">Fallback ladder</h2>
        <p className="max-w-[680px] text-body leading-[1.5] text-muted">
          The order Graphban tries when it routes work. The project policy sets the ladder. A profile can reorder or
          remove rungs, but can't add back a rung the policy removed.
        </p>
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(320px,1fr))] items-start gap-3.5">
        <div data-testid="ladder-policy" className="flex flex-col rounded-[12px] border border-line-2 bg-surface">
          <div className="flex flex-wrap items-baseline gap-2.5 border-b border-line-2/70 px-4 py-3">
            <h3 className="text-[13.5px] font-semibold">Project policy</h3>
            <span className="font-mono text-[10.5px] text-muted">applies to everyone</span>
          </div>
          <ol className="flex flex-col gap-2 p-2.5">
            {allowed.map((r, i) => (
              <li key={r.rung} data-testid="ladder-policy-rung" data-rung={r.rung} className={cn(tile, liveTile, "grid-cols-[28px_minmax(0,1fr)]")}>
                <Num n={String(i + 1)} />
                <span className="flex min-w-0 flex-col gap-[3px]"><Rung label={r.label} description={r.description} /></span>
              </li>
            ))}
            {policyRemoved.map((r) => (
              <li key={r.rung} data-testid="ladder-policy-rung" data-rung={r.rung} data-removed="true" className={cn(tile, gone, "grid-cols-[28px_minmax(0,1fr)]")}>
                <Num n="—" />
                <span className="flex min-w-0 flex-col gap-[3px]">
                  <Rung label={r.label} description={r.description} struck />
                  <Tag>REMOVED BY PROJECT POLICY</Tag>
                </span>
              </li>
            ))}
          </ol>
        </div>
        <div data-testid="ladder-override" className="flex flex-col rounded-[12px] border border-line-2 bg-surface">
          <div className="flex flex-wrap items-baseline gap-2.5 border-b border-line-2/70 px-4 py-3">
            <h3 data-testid="ladder-override-heading" className="text-[13.5px] font-semibold">{heading}</h3>
            <span className="font-mono text-[10.5px] text-muted">profile · {claims}</span>
            <span className="flex-1" />
            {data.override && (
              <button type="button" data-testid="ladder-match-policy" className="text-[12px] text-muted hover:text-fg disabled:opacity-50"
                      disabled={save.isPending} onClick={() => write(null)}>Match policy</button>
            )}
          </div>
          <ol className="flex flex-col gap-2 p-2.5">
            {live.map((rung, i) => {
              const r = byRung.get(rung);
              if (!r) return null;
              return (
                <li key={rung} data-testid="ladder-override-rung" data-rung={rung} className={cn(tile, liveTile, "grid-cols-[28px_minmax(0,1fr)_auto]")}>
                  <Num n={String(i + 1)} />
                  <span className="flex min-w-0 flex-col gap-[3px]"><Rung label={r.label} description={r.description} /></span>
                  <span className="flex gap-1">
                    <button type="button" aria-label={`Move ${r.label} up`} className={cn(btn, "w-7 px-0")} disabled={save.isPending || i === 0} onClick={() => move(i, -1)}>
                      <ChevronUp size={12} strokeWidth={2.2} />
                    </button>
                    <button type="button" aria-label={`Move ${r.label} down`} className={cn(btn, "w-7 px-0")} disabled={save.isPending || i === live.length - 1} onClick={() => move(i, 1)}>
                      <ChevronDown size={12} strokeWidth={2.2} />
                    </button>
                    <button type="button" data-testid="ladder-remove" aria-label={`Remove ${r.label} rung`} className={btn} disabled={save.isPending}
                            onClick={() => write(live.filter((x) => x !== rung))}>Remove</button>
                  </span>
                </li>
              );
            })}
            {userRemoved.map((r) => (
              <li key={r.rung} data-testid="ladder-override-rung" data-rung={r.rung} data-user-removed="true" className={cn(tile, gone, "grid-cols-[28px_minmax(0,1fr)_auto]")}>
                <Num n="—" />
                <span className="flex min-w-0 flex-col gap-[3px]">
                  <Rung label={r.label} description={r.description} struck />
                  <Tag>REMOVED BY {person ? person.toUpperCase() : "YOU"}</Tag>
                </span>
                <button type="button" data-testid="ladder-restore" className={btn} disabled={save.isPending}
                        onClick={() => write([...live, r.rung])}>Restore</button>
              </li>
            ))}
            {policyRemoved.map((r) => (
              <li key={r.rung} data-testid="ladder-override-rung" data-rung={r.rung} data-policy-removed="true" className={cn(tile, gone, "grid-cols-[28px_minmax(0,1fr)_auto]")}>
                <Num n="—" />
                <span className="flex min-w-0 flex-col gap-[3px]">
                  <Rung label={r.label} description={r.description} struck />
                  <Tag>REMOVED BY PROJECT POLICY</Tag>
                </span>
                <span data-testid="ladder-cant-add" title="Only the project policy can add this rung back"
                      className="inline-flex h-7 items-center gap-[5px] rounded-[7px] border border-dashed border-line px-[9px] text-[12px] text-muted">
                  <Lock size={11} />Can't add back
                </span>
              </li>
            ))}
          </ol>
          {save.isError && (
            <p role="alert" data-testid="ladder-refusal" className="mx-4 mb-3 text-body text-muted">{refusalText(save.error)}</p>
          )}
        </div>
      </div>
    </div>
  );
}
