import * as React from "react";
import { Link } from "react-router-dom";

import { FETCH_FAILED, PlannerError, TableSkeleton } from "@/components/planner/PlannerStates";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useClearFleetTierMap, useFleetTierMap, useSaveFleetTierMap } from "@/lib/queries";
import type { FleetMatrixRow, FleetTierCell } from "@/lib/types";

/**
 * Committed harness × model × tier catalog (GRPH-866).
 *
 * Facts only. The Observe → Harness page is where rates and rankings live; this table
 * names what can run, and a link is how you get to how it turned out.
 */
export function MatrixTable({ rows, harnessHref }: {
  rows: FleetMatrixRow[];
  harnessHref: string;
}) {
  return (
    <section className="mb-7" data-testid="fleet-matrix">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 className="text-lead font-semibold tracking-tight">Harness catalog</h2>
          <p className="mt-0.5 text-[12px] text-muted">
            What <code className="font-mono text-small">spawn(tier=)</code> can resolve to.
            Status moves by a commit, not by this page.
          </p>
        </div>
        <Link
          to={harnessHref}
          className="shrink-0 text-[12px] text-fg-2 underline-offset-2 hover:underline"
          data-testid="fleet-matrix-observe"
        >
          Performance and rankings
        </Link>
      </div>
      {rows.length === 0 ? (
        <p className="rounded-[11px] border border-dashed border-line-2 px-3 py-6 text-center text-body text-muted">
          The catalog has not been served. That is not an empty matrix.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-[11px] border border-line-2">
          <table className="w-full text-left text-body">
            <thead className="border-b border-line-2 bg-surface-2 font-mono text-[10px] uppercase tracking-wide text-faint">
              <tr>
                <th className="px-3 py-2 font-medium">Harness</th>
                <th className="px-3 py-2 font-medium">Model</th>
                <th className="px-3 py-2 font-medium">Tier</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Cost</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.harness}:${row.model}:${row.tier}`}
                    className="border-b border-line-2 last:border-0">
                  <td className="px-3 py-2 font-mono text-[12px]">{row.harness}</td>
                  <td className="px-3 py-2 font-mono text-[12px] text-muted">
                    {row.model || "—"}
                  </td>
                  <td className="px-3 py-2 font-mono text-[12px]">{row.tier}</td>
                  <td className="px-3 py-2">
                    <span className={cn(
                      "rounded-md border px-1.5 py-0.5 font-mono text-[10px]",
                      row.status === "verified" && "border-accent/40 text-accent",
                      row.status === "failed" && "border-[color:var(--color-st-blocked)]/40 text-[color:var(--color-st-blocked)]",
                      row.status === "unregistered" && "border-line-2 text-faint",
                      (row.status === "unverified" || !["verified", "failed", "unregistered"].includes(row.status))
                        && "border-line-2 text-muted",
                    )}>
                      {row.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono text-small text-muted">
                    {row.cost_class}{row.local ? " · local" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/**
 * The `<select>` value that means "no override — run the packaged model".
 *
 * Empty model names are filtered out of every picklist below, so this sentinel can never
 * collide with a real choice, and it is the only value that PUTs `model: null`.
 */
const USE_PACKAGED = "";

/** Harness and tier names both contain `-` and `_`, so the join uses a NUL. */
function cellKey(harness: string, tier: string): string {
  return `${harness}\u0000${tier}`;
}

/** First-seen order, deduped. The tier list is DERIVED, never hardcoded: a third tier the
 *  catalog starts serving has to appear here without a frontend change. */
function inOrder(values: string[]): string[] {
  const out: string[] = [];
  for (const v of values) if (v && !out.includes(v)) out.push(v);
  return out;
}

/** Every model this cell can be pointed at: the catalog's list plus the packaged model, so a
 *  packaged model the catalog dropped is still selectable. Blanks are dropped — see
 *  {@link USE_PACKAGED}. */
function picklist(cell: FleetTierCell): string[] {
  return inOrder([...cell.models, cell.packaged_model]);
}

/** The draft's own answer to "what will run", which is the server's `effective_model` while
 *  nothing is pending and the edit the operator is looking at while something is. */
function effectiveOf(cell: FleetTierCell, draft: string | null): string {
  return draft ?? cell.packaged_model;
}

/**
 * Tier map — which model each harness runs when (GRPH-1003, design `Build.dc.html`).
 *
 * The catalog table above is the facts view and stays read-only; this is the control the
 * design draws and the table was missing. An override is a per-deployment choice, never a
 * commit, so it is stored beside the matrix and every cell keeps showing the packaged model
 * it falls back to.
 *
 * The two links go to the same place on purpose. "Grading rules" is a section of the Harness
 * page's Guidance tab (`GuidanceTab.tsx`, `data-testid="guidance-grading-rules"`), and that
 * tab is local `useState` in `HarnessView` — there is no route, segment or query param that
 * addresses it. Linking to `harness` and saying so beats inventing `/harness/grading`, which
 * would 404 or, worse, silently render the Performance tab.
 */
export function TierMapPanel({ projectId, harnessHref }: {
  projectId: string;
  harnessHref: string;
}) {
  const query = useFleetTierMap(projectId);
  const save = useSaveFleetTierMap(projectId);
  const clear = useClearFleetTierMap(projectId);
  // Bumped on a successful clear so the form remounts even when the server's answer carries
  // the same overrides it already had (nothing was saved to clear) — without it an unsaved
  // draft would survive the button whose job is to discard it.
  const [resetTick, setResetTick] = React.useState(0);

  const cells = query.data?.cells ?? [];
  // The draft's baseline, not the response's identity: a plain refetch that changes no
  // override must not wipe an edit in progress, while a save or a clear must. Same reason
  // `LogExportPanel` keys its form — an effect syncing the draft from `data` gets both wrong.
  const baseline = `${cells.map((c) => `${c.harness}|${c.tier}|${c.override ?? ""}`).join(",")}#${resetTick}`;

  return (
    <section className="mb-7" data-testid="fleet-tier-map">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 className="text-[14px] font-semibold tracking-tight">Tier map</h2>
          <p className="mt-0.5 text-[12px] text-muted">Which model each harness runs when</p>
        </div>
        <div className="flex shrink-0 items-center gap-3 text-[12px]">
          <Link
            to={harnessHref}
            className="text-fg-2 underline-offset-2 hover:underline"
            data-testid="tier-map-grading-link"
          >
            Grading rules →
          </Link>
          <Link
            to={harnessHref}
            className="text-fg-2 underline-offset-2 hover:underline"
            data-testid="tier-map-performance-link"
          >
            Performance and rankings →
          </Link>
        </div>
      </div>

      {query.isError && !query.data ? (
        // NOT the empty state. A read that failed has no idea what is mapped, and "no harness
        // to map" underneath a failed request is the absence-reads-as-clean defect (PRD-47 S1).
        <PlannerError
          message={`${FETCH_FAILED} Which model each harness runs was not read — this is not "nothing overridden".`}
          onRetry={() => { void query.refetch(); }}
        />
      ) : !query.data && query.isFetching ? (
        <TableSkeleton rows={3} columns={3} />
      ) : cells.length === 0 ? (
        // Also where a page with no active project lands: the query is gated off, nothing was
        // served, and that really is "no catalog" rather than a failure.
        <p
          className="rounded-[11px] border border-dashed border-line-2 px-3 py-6 text-center text-[12.5px] text-muted"
          data-testid="tier-map-empty"
        >
          No harness to map until the catalog is served.
        </p>
      ) : (
        <TierMapForm
          key={baseline}
          cells={cells}
          saving={save.isPending}
          clearing={clear.isPending}
          saveError={save.isError
            ? errorDetail(save.error, "could not save the tier map") : ""}
          clearError={clear.isError
            ? errorDetail(clear.error, "could not clear the tier map") : ""}
          onSave={(payload) => save.mutate(payload)}
          onClear={() => clear.mutate(undefined, {
            onSuccess: () => setResetTick((n) => n + 1),
          })}
        />
      )}
    </section>
  );
}

type TierMapPayload = { harness: string; tier: string; model: string | null }[];

function TierMapForm({ cells, saving, clearing, saveError, clearError, onSave, onClear }: {
  cells: FleetTierCell[];
  saving: boolean;
  clearing: boolean;
  saveError: string;
  clearError: string;
  onSave: (payload: TierMapPayload) => void;
  onClear: () => void;
}) {
  // `null` is "use the packaged model". Every cell gets a key on mount, so the draft is total
  // and a missing key can never be mistaken for "no override".
  const [draft, setDraft] = React.useState<Record<string, string | null>>(() =>
    Object.fromEntries(cells.map((c) => [cellKey(c.harness, c.tier), c.override ?? null])));
  const [note, setNote] = React.useState("");

  const tiers = React.useMemo(() => inOrder(cells.map((c) => c.tier)), [cells]);
  const harnesses = React.useMemo(() => inOrder(cells.map((c) => c.harness)), [cells]);
  const byKey = React.useMemo(
    () => new Map(cells.map((c) => [cellKey(c.harness, c.tier), c])), [cells]);

  const dirty = cells.filter((c) => (draft[cellKey(c.harness, c.tier)] ?? null) !== (c.override ?? null));
  const savedOverrides = cells.filter((c) => c.override !== null).length;
  const pending = dirty.length > 0;

  function choose(harness: string, tier: string, value: string) {
    setDraft((prev) => ({
      ...prev,
      [cellKey(harness, tier)]: value === USE_PACKAGED ? null : value,
    }));
    setNote("");
  }

  /** Fill every MEASURED cell with the model grading picked. An unmeasured cell is left
   *  exactly as it was and named as unmeasured — a guess there would be an override nobody
   *  chose, wearing the authority of a measurement that does not exist (PRD-47 G3). */
  function inheritGrading() {
    const measured = cells.filter((c) => c.graded_model !== null);
    setDraft((prev) => {
      const next = { ...prev };
      for (const c of measured) next[cellKey(c.harness, c.tier)] = c.graded_model;
      return next;
    });
    const left = cells.length - measured.length;
    setNote(measured.length === 0
      ? "Nothing inherited — no cell in this catalog is measured yet."
      : `Inherited the graded model for ${measured.length} cell${measured.length === 1 ? "" : "s"}.`
        + (left > 0
          ? ` ${left} not measured — left as ${left === 1 ? "it was" : "they were"}, nothing invented.`
          : ""));
  }

  function save() {
    setNote("");
    // Full state, not a patch: every cell is sent, so a cell the operator put back on
    // "packaged" is cleared by the same write that saves the ones they changed.
    onSave(cells.map((c) => ({
      harness: c.harness,
      tier: c.tier,
      model: draft[cellKey(c.harness, c.tier)] ?? null,
    })));
  }

  return (
    <>
      <div className="overflow-x-auto rounded-[11px] border border-line-2">
        <table className="w-full text-left text-[12.5px]">
          <thead className="border-b border-line-2 bg-surface-2 font-mono text-[10px] uppercase tracking-wide text-faint">
            <tr>
              <th className="px-3 py-2 font-medium">Harness</th>
              {tiers.map((tier) => (
                <th key={tier} className="px-3 py-2 font-medium">Tier · {tier}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {harnesses.map((harness) => (
              <tr key={harness} className="border-b border-line-2 align-top last:border-0">
                <th scope="row" className="px-3 py-2 font-mono text-[12px] font-normal">
                  {harness}
                </th>
                {tiers.map((tier) => {
                  const cell = byKey.get(cellKey(harness, tier));
                  if (!cell) {
                    // The catalog has no model for this harness at this tier. Not a cleared
                    // override and not an error — it is a square the matrix does not cover.
                    return (
                      <td key={tier} className="px-3 py-2 text-[11px] text-faint">
                        not in catalog
                      </td>
                    );
                  }
                  const value = draft[cellKey(harness, tier)] ?? null;
                  const overridden = value !== null;
                  const unsaved = value !== (cell.override ?? null);
                  const choices = picklist(cell);
                  return (
                    <td key={tier} className="px-3 py-2">
                      <div className="flex min-w-[13rem] flex-col gap-1">
                        <select
                          aria-label={`Model for ${harness} at ${tier}`}
                          data-testid="tier-map-select"
                          className={cn(
                            "w-full rounded-[8px] border bg-surface-2 px-2 py-1 font-mono text-[11.5px] outline-none",
                            "[@media(hover:hover)_and_(pointer:fine)]:hover:border-control-hover",
                            "focus-visible:border-control-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
                            // An overridden cell is the one thing on this panel that is not the
                            // committed default, so it gets the accent border rather than a dot.
                            overridden ? "border-accent text-fg" : "border-control text-muted",
                          )}
                          value={value ?? USE_PACKAGED}
                          onChange={(e) => choose(harness, tier, e.target.value)}
                        >
                          <option value={USE_PACKAGED}>
                            packaged · {cell.packaged_model || "nothing committed"}
                          </option>
                          {choices.map((model) => (
                            <option key={model} value={model}>{model}</option>
                          ))}
                          {value !== null && !choices.includes(value) && (
                            // The draft holds a model this cell's picklist does not name: a saved
                            // override the catalog stopped serving, or a graded model Inherit just
                            // wrote. Without this option the select paints its FIRST entry while
                            // the effective line beside it says something else, and the operator
                            // can neither see nor undo the value they are looking at.
                            <option value={value}>{value} · not in this catalog</option>
                          )}
                        </select>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span
                            className={cn("font-mono text-[11px]", overridden ? "text-accent" : "text-fg-2")}
                            data-testid="tier-map-effective"
                          >
                            {effectiveOf(cell, value) || "—"}
                          </span>
                          {overridden && (
                            <span className="rounded-md border border-accent/40 px-1 font-mono text-[9.5px] text-accent">
                              override
                            </span>
                          )}
                          {unsaved && (
                            <span className="rounded-md border border-control px-1 font-mono text-[9.5px] text-muted">
                              unsaved
                            </span>
                          )}
                        </div>
                        <span
                          className={cn("text-[10.5px]", cell.graded_model ? "text-faint" : "text-muted")}
                          data-testid="tier-map-graded"
                        >
                          {cell.graded_model ? `graded · ${cell.graded_model}` : "not measured"}
                        </span>
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={inheritGrading} data-testid="tier-map-inherit">
          Inherit from performance grading
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={onClear}
          disabled={clearing}
          loading={clearing}
          data-testid="tier-map-clear"
        >
          Clear overrides
        </Button>
        <Button
          size="sm"
          onClick={save}
          disabled={!pending || saving}
          loading={saving}
          data-testid="tier-map-save"
        >
          Save tier map
        </Button>
      </div>

      {/* What is saved, said out loud. "No overrides" is a state worth naming, because the
          quiet version of this panel — every cell on its packaged model — otherwise looks
          identical to a map nobody has ever opened. */}
      <p className="mt-2 text-[11.5px] text-muted" role="status" data-testid="tier-map-status">
        {pending
          ? `Unsaved changes in ${dirty.length} cell${dirty.length === 1 ? "" : "s"}.`
          : savedOverrides > 0
            ? `${savedOverrides} saved override${savedOverrides === 1 ? "" : "s"} · nothing unsaved.`
            : "No overrides saved — every cell runs its packaged model."}
      </p>
      {note && <p className="mt-1 text-[11.5px] text-muted" role="status" data-testid="tier-map-note">{note}</p>}
      {saveError && (
        <div data-testid="tier-map-save-error">
          <PlannerError message={saveError} onRetry={save} />
        </div>
      )}
      {clearError && (
        <div data-testid="tier-map-clear-error">
          <PlannerError message={clearError} onRetry={onClear} />
        </div>
      )}
    </>
  );
}
