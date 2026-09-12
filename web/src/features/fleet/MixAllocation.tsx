import * as React from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { errorDetail } from "@/lib/errors";
import type { FleetMatrixRow, FleetProfile } from "@/lib/types";

function harnessNames(rows: FleetMatrixRow[]): string[] {
  const seen: string[] = [];
  for (const row of rows) {
    if (row.status === "unregistered") continue;
    if (!seen.includes(row.harness)) seen.push(row.harness);
  }
  return seen;
}

function percentsFromMix(names: string[], mix: Record<string, number> | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const name of names) out[name] = 0;
  if (!mix) return out;
  for (const name of names) {
    const share = mix[name];
    if (typeof share === "number" && share > 0) out[name] = Math.round(share * 100);
  }
  return out;
}

/**
 * Percentage allocation across catalog harnesses (GRPH-865 / GRPH-866).
 *
 * Empty mix is winner-take-all, not a zeroed map. Saving all zeros is refused rather than
 * stored as "nobody". Recent launch share is shown beside the target when n ≥ 3.
 */
export function MixAllocation({ projectId, profile, rows, recent, onSaved }: {
  projectId: string;
  profile: FleetProfile | null;
  rows: FleetMatrixRow[];
  recent?: { n: number; by_harness: Record<string, number>; unreported: number };
  onSaved: () => void;
}) {
  const names = React.useMemo(() => harnessNames(rows), [rows]);
  const [enabled, setEnabled] = React.useState(!!profile?.mix);
  const [percents, setPercents] = React.useState<Record<string, number>>(
    () => percentsFromMix(names, profile?.mix),
  );
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    setEnabled(!!profile?.mix);
    setPercents(percentsFromMix(names, profile?.mix));
  }, [profile, names]);

  const total = names.reduce((sum, name) => sum + (percents[name] ?? 0), 0);
  const n = recent?.n ?? 0;

  async function save() {
    setError(""); setNote("");
    if (enabled && total <= 0) {
      setError("give at least one harness a share, or turn allocation off");
      return;
    }
    const mix = enabled
      ? Object.fromEntries(names.filter((name) => (percents[name] ?? 0) > 0)
          .map((name) => [name, (percents[name] ?? 0) / 100]))
      : null;
    try {
      const saved = await api.saveFleetProfile({
        project_id: profile?.scope === "project" ? projectId : null,
        defaults: profile?.defaults ?? [],
        weights: profile?.weights ?? {},
        excludes: profile?.excludes ?? [],
        budget_tokens: profile?.budget_tokens ?? null,
        mix,
      });
      setNote(saved.scope === "project"
        ? "Saved as the override for this project."
        : "Saved as your default allocation.");
      onSaved();
    } catch (e) {
      setError(errorDetail(e, "could not save the mix"));
    }
  }

  return (
    <section className="mb-7" data-testid="fleet-mix">
      <h2 className="text-[14px] font-semibold tracking-tight">Allocation</h2>
      <p className="mb-3 mt-0.5 text-[12px] text-muted">
        Share of recent spawns across harnesses that can actually resolve. Off means the
        scorer always picks the winner. Unregistered adapters are not offered a share.
      </p>
      <label className="mb-3 flex items-center gap-2 text-[12.5px]">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
          aria-label="Allocate by share"
        />
        Allocate by share
      </label>
      {names.length === 0 ? (
        <p className="rounded-[11px] border border-dashed border-line-2 px-3 py-4 text-[12.5px] text-muted">
          No mixable harness in the catalog.
        </p>
      ) : (
        <div className="space-y-2 rounded-[11px] border border-line-2 p-3">
          {names.map((name) => {
            const actual = n >= 3 && recent
              ? Math.round(((recent.by_harness[name] ?? 0) / n) * 100)
              : null;
            return (
              <label key={name} className="grid grid-cols-[7rem_1fr_3rem_auto] items-center gap-2 text-[12.5px]">
                <span className="font-mono text-[12px]">{name}</span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={5}
                  disabled={!enabled}
                  aria-label={`Share ${name}`}
                  value={percents[name] ?? 0}
                  onChange={(e) => setPercents((prev) => ({ ...prev, [name]: Number(e.target.value) }))}
                />
                <span className="font-mono text-[11px] text-muted tabular-nums">
                  {percents[name] ?? 0}%
                </span>
                <span className="text-[11px] text-faint">
                  {actual === null ? (n > 0 && n < 3 ? `n=${n}, unmeasured` : "") : `now ${actual}%`}
                </span>
              </label>
            );
          })}
          <p className="pt-1 text-[11px] text-muted">
            Sum {total}%. The server normalises on save.
            {n > 0 ? ` Last ${n} matrix launch${n === 1 ? "" : "es"} in this project.` : ""}
          </p>
        </div>
      )}
      <div className="mt-2">
        <Button size="sm" onClick={() => { void save(); }}>Save allocation</Button>
      </div>
      {note && <p className="mt-2 text-[12px] text-muted" role="status">{note}</p>}
      {error && <p className="mt-2 text-[12px] text-red-500" role="alert">{error}</p>}
    </section>
  );
}
