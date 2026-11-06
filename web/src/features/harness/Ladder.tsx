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

const Num = ({ n }: { n: string }) => <span className="w-5 font-mono text-small text-faint">{n}</span>;
const btn = "rounded-md border border-control px-2 py-0.5 text-small hover:bg-surface-3 disabled:opacity-50";

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
  const live = data.override ? data.override.map((r) => r.rung) : data.effective.map((r) => r.rung);
  const allowed = data.policy.filter((r) => !r.removed);
  const byRung = new Map<string, HarnessLadderRung>(data.policy.map((r) => [r.rung, r]));
  const userRemoved = data.override ? allowed.filter((r) => !live.includes(r.rung)) : [];
  const policyRemoved = data.policy.filter((r) => r.removed);
  const write = (next: string[] | null) => save.mutate(next);
  const move = (i: number, d: number) => {
    const o = live.slice(); const j = i + d;
    if (j < 0 || j >= o.length) return;
    [o[i], o[j]] = [o[j], o[i]];
    write(o);
  };

  return (
    <div data-testid="capacity-ladder">
      <div className="text-[13px] font-medium">Fallback ladder</div>
      <p className="text-body text-muted">
        The order Graphban tries when it routes work. The project policy sets the ladder. A profile can reorder or
        remove rungs, but can't add back a rung the policy removed.
      </p>
      <div className="mt-2 grid grid-cols-2 gap-4">
        <div data-testid="ladder-policy">
          <div className="text-small text-faint">Project policy · applies to everyone</div>
          {allowed.map((r, i) => (
            <div key={r.rung} data-testid="ladder-policy-rung" data-rung={r.rung} className="flex gap-2 py-1">
              <Num n={String(i + 1)} />
              <div><div className="font-mono text-small">{r.label}</div><div className="text-small text-muted">{r.description}</div></div>
            </div>
          ))}
          {policyRemoved.map((r) => (
            <div key={r.rung} data-testid="ladder-policy-rung" data-rung={r.rung} data-removed="true" className="flex gap-2 py-1 opacity-70">
              <Num n="—" />
              <div>
                <div className="font-mono text-small line-through">{r.label}</div>
                <div className="text-small text-muted line-through">{r.description}</div>
                <span className="font-mono text-meta text-muted">REMOVED BY PROJECT POLICY</span>
              </div>
            </div>
          ))}
        </div>
        <div data-testid="ladder-override">
          <div className="flex items-baseline justify-between">
            <span className="text-small text-faint"><span data-testid="ladder-override-heading">{heading}</span> · profile · {claims}</span>
            <button type="button" data-testid="ladder-match-policy" className={btn} disabled={save.isPending}
                    onClick={() => write(null)}>Match policy</button>
          </div>
          {live.map((rung, i) => {
            const r = byRung.get(rung);
            if (!r) return null;
            return (
              <div key={rung} data-testid="ladder-override-rung" data-rung={rung} className="flex items-start gap-2 py-1">
                <Num n={String(i + 1)} />
                <div className="flex-1"><div className="font-mono text-small">{r.label}</div><div className="text-small text-muted">{r.description}</div></div>
                <button type="button" aria-label={`Move ${r.label} up`} className={btn} disabled={save.isPending || i === 0} onClick={() => move(i, -1)}>↑</button>
                <button type="button" aria-label={`Move ${r.label} down`} className={btn} disabled={save.isPending || i === live.length - 1} onClick={() => move(i, 1)}>↓</button>
                <button type="button" data-testid="ladder-remove" className={btn} disabled={save.isPending}
                        onClick={() => write(live.filter((x) => x !== rung))}>Remove</button>
              </div>
            );
          })}
          {userRemoved.map((r) => (
            <div key={r.rung} data-testid="ladder-override-rung" data-rung={r.rung} data-user-removed="true" className="flex items-start gap-2 py-1 opacity-70">
              <Num n="—" />
              <div className="flex-1">
                <div className="font-mono text-small line-through">{r.label}</div>
                <span className="font-mono text-meta text-muted">REMOVED BY {person ? person.toUpperCase() : "YOU"}</span>
              </div>
              <button type="button" data-testid="ladder-restore" className={btn} disabled={save.isPending}
                      onClick={() => write([...live, r.rung])}>Restore</button>
            </div>
          ))}
          {policyRemoved.map((r) => (
            <div key={r.rung} data-testid="ladder-override-rung" data-rung={r.rung} data-policy-removed="true" className="flex items-start gap-2 py-1 opacity-70">
              <Num n="—" />
              <div className="flex-1"><div className="font-mono text-small line-through">{r.label}</div></div>
              <span data-testid="ladder-cant-add" className="text-small text-muted">Can't add back</span>
            </div>
          ))}
          {save.isError && (
            <p role="alert" data-testid="ladder-refusal" className="mt-1 text-body text-muted">{refusalText(save.error)}</p>
          )}
        </div>
      </div>
    </div>
  );
}
