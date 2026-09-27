import { useState } from "react";
import { Check, ChevronRight, Play, X } from "lucide-react";

import { useHarnessCards, useHarnessProbeCandidates, useMarkHarnessCard, useStartHarnessProbeRun } from "@/lib/queries";
import type { HarnessCard, HarnessCardCell, HarnessProbeSuggestion, HarnessReport } from "@/lib/types";

type ProbeCandidate = { id: string; key: string; title: string; capabilities: string[]; touchpoints: string[] };

/**
 * PRD-47 S12 — Changes & probes tab.
 *
 * Recommendations as drafts with their framing — "Accepting one records that you have seen
 * it… This page changes nothing" — the replay evidence, accept and dismiss, and the probe
 * panel with its per-attempt token estimate.
 */
export function ChangesProbesTab({ projectId, data }: { projectId: string; data: HarnessReport }) {
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4 p-5">
      <Recommendations projectId={projectId} floor={data.floor} windowDays={data.window_days} />
      <ProbePanel
        projectId={projectId}
        suggestions={data.probe_suggestions ?? []}
        labels={data.capability_set?.labels}
      />
    </div>
  );
}

function Recommendations({ projectId, floor, windowDays }: { projectId: string; floor: number; windowDays: number }) {
  const { data, isLoading } = useHarnessCards(projectId);
  const mark = useMarkHarnessCard(projectId);

  return (
    <section>
      <h2 className="text-[14px] font-semibold tracking-tight">Recommended changes</h2>
      <p className="mb-3 mt-0.5 text-[12px] text-muted">
        Accepting one records that you have seen it — it stays quiet until its numbers move.
        This page changes nothing: R1 and R2 accept by producing text for a commit somebody
        makes; R3 and R4 accept by changing a profile or policy through their own screens.
      </p>
      {isLoading || !data ? (
        <div className="text-[12.5px] text-muted">Loading recommendations…</div>
      ) : data.cards.length === 0 ? (
        <div
          data-testid="harness-no-cards"
          className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
        >
          No recommended changes. R1–R6 fire on cells above the {floor}-attempt
          floor; nothing in the last {windowDays} days met one.
        </div>
      ) : (
        <div className="flex flex-col gap-2" data-testid="harness-cards">
          {data.cards.map((card) => (
            <CardRow
              key={card.key}
              card={card}
              onMark={(action) =>
                mark.mutate({ card_key: card.key, evidence_hash: card.evidence_hash, action })
              }
            />
          ))}
        </div>
      )}
    </section>
  );
}

function CardRow({
  card,
  onMark,
}: {
  card: HarnessCard;
  onMark: (action: "accept" | "dismiss") => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div
      data-testid="harness-card"
      data-rule={card.rule}
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3"
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 rounded-full border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-faint">
          {card.rule}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium">{card.title}</div>
          <p className="mt-0.5 text-[12.5px] text-muted">{card.detail}</p>
          {Array.isArray(card.draft?.projects) && (card.draft.projects as string[]).length > 0 && (
            <p data-testid="harness-card-projects" className="mt-1 font-mono text-[11px] text-faint">
              projects: {(card.draft.projects as string[]).join(", ")}
            </p>
          )}
          {card.draft?.probe === true && (
            <p data-testid="harness-card-probe" className="mt-1 font-mono text-[11px] text-faint">
              probe samples contributed
            </p>
          )}
          {card.previously?.evidence_changed && (
            <p data-testid="harness-card-returned" className="mt-1 text-[12px] text-[#f0b450]">
              You {card.previously.state} this on{" "}
              {new Date(card.previously.at).toLocaleDateString()} — the evidence has changed
              since.
            </p>
          )}
          <p data-testid="harness-card-replay" className="mt-1.5 font-mono text-[11px] text-faint">
            {card.replay?.summary}
            {(card.replay?.moves?.length ?? 0) > 0 &&
              ` — ${card.replay.moves
                .map((m) => `${m.count} from ${m.from} to ${m.to}`)
                .join(", ")}`}
            {card.replay?.truncated && " (replay truncated)"}
          </p>
        </div>
        <div className="flex flex-none gap-1.5">
          <button
            type="button"
            aria-label={`Accept ${card.title}`}
            data-testid="harness-accept"
            onClick={() => onMark("accept")}
            className="inline-flex items-center gap-1 rounded-[8px] border border-line-2 px-2 py-1 text-[11.5px] hover:bg-surface"
          >
            <Check size={12} /> Accept
          </button>
          <button
            type="button"
            aria-label={`Dismiss ${card.title}`}
            data-testid="harness-dismiss"
            onClick={() => onMark("dismiss")}
            className="inline-flex items-center gap-1 rounded-[8px] border border-line-2 px-2 py-1 text-[11.5px] text-muted hover:bg-surface"
          >
            <X size={12} /> Dismiss
          </button>
        </div>
      </div>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="harness-card-expand"
        aria-expanded={open}
        className="mt-2 inline-flex items-center gap-1 font-mono text-[10.5px] text-faint hover:text-muted"
      >
        <ChevronRight size={11} className={open ? "rotate-90" : ""} />
        evidence, what it does not cover, and how to apply it
      </button>

      {open && (
        <div className="mt-2 flex flex-col gap-2 border-t border-line-2 pt-2">
          <Cells label="fired on" cells={card.cells} testid="harness-card-cells" />
          {card.siblings.length > 0 && (
            <Cells
              label="did not fire on — what accepting this generalises over"
              cells={card.siblings}
              testid="harness-card-siblings"
            />
          )}
          <div className="font-mono text-[10.5px] text-faint">
            thresholds:{" "}
            {Object.entries(card.thresholds)
              .map(([k, v]) => `${k} ${v}`)
              .join(" · ")}
          </div>
          <div
            data-testid="harness-card-apply"
            className="rounded-[8px] border border-line-2 bg-surface px-2.5 py-2 font-mono text-[11px]"
          >
            <div className="text-faint">apply by hand — this page changes nothing:</div>
            <div className="mt-1 break-all">{String(card.draft.where ?? "")}</div>
            {typeof card.draft.evidence_line === "string" && (
              <div className="mt-1 break-all">{card.draft.evidence_line}</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Cells({
  label,
  cells,
  testid,
}: {
  label: string;
  cells: HarnessCardCell[];
  testid: string;
}) {
  return (
    <div data-testid={testid}>
      <div className="font-mono text-[10.5px] text-faint">{label}</div>
      <ul className="mt-1 flex flex-col gap-0.5">
        {cells.map((c, i) => (
          <li key={i} className="font-mono text-[11px]">
            {c.cell.vendor}
            {c.cell.model ? `:${c.cell.model}` : ""} · {c.cell.capability}/
            {c.cell.size_band} · {c.signed_off}/{c.finished}
            {c.below_floor ? " (below the floor)" : ""}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ProbePanel({
  projectId,
  suggestions,
  labels,
}: {
  projectId: string;
  suggestions: HarnessProbeSuggestion[];
  labels?: Record<string, string>;
}) {
  const { data, isLoading } = useHarnessProbeCandidates(projectId);
  const startRun = useStartHarnessProbeRun(projectId);

  const [pickedSuggestion, setPickedSuggestion] = useState<number | null>(null);
  const [pickedItems, setPickedItems] = useState<Record<string, string[]>>({});
  const [error, setError] = useState<string | null>(null);

  if (isLoading) return null;
  if (!data) return null;

  const hasCandidates = Object.keys(data.by_leaf).length > 0;
  if (!hasCandidates && suggestions.length === 0) {
    return (
      <section>
        <h2 className="text-[14px] font-semibold tracking-tight">Probe</h2>
        <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3 text-[12.5px] text-muted">
          No probe candidates. A probe needs a closed item with a red sabotage and a measured
          cell to test against — nothing in the window qualifies yet.
        </div>
      </section>
    );
  }

  const suggestion = pickedSuggestion !== null ? suggestions[pickedSuggestion] : null;

  const fallbackLeaves = new Set<string>();
  for (const [fam, group] of Object.entries(data.by_family)) {
    if (group.fallback) {
      for (const leaf of group.leaf_ready) fallbackLeaves.add(leaf);
      void fam;
    }
  }

  const groups: { key: string; label: string; items: ProbeCandidate[] }[] = [];
  for (const [fam, group] of Object.entries(data.by_family)) {
    if (group.fallback && group.items.length > 0) {
      groups.push({ key: fam, label: labels?.[fam] ?? `${fam} (family)`, items: group.items });
    }
  }
  for (const [leaf, items] of Object.entries(data.by_leaf)) {
    if (fallbackLeaves.has(leaf)) continue;
    if (items.length > 0) {
      groups.push({ key: leaf, label: labels?.[leaf] ?? leaf, items });
    }
  }

  const totalPicked = Object.values(pickedItems).reduce((s, a) => s + a.length, 0);

  const toggleItem = (groupKey: string, itemId: string) => {
    setPickedItems((prev) => {
      const cur = prev[groupKey] ?? [];
      if (cur.includes(itemId)) return { ...prev, [groupKey]: cur.filter((id) => id !== itemId) };
      if (cur.length >= 3) return prev;
      return { ...prev, [groupKey]: [...cur, itemId] };
    });
  };

  const handleStart = () => {
    if (!suggestion) return;
    setError(null);
    for (const [cap, ids] of Object.entries(pickedItems)) {
      if (ids.length === 0) continue;
      startRun.mutate(
        {
          vendor: suggestion.vendor,
          model: suggestion.model,
          capability: cap,
          item_ids: ids,
          trigger: suggestion.trigger,
          binary_version: suggestion.binary_version,
        },
        {
          onSuccess: () => {
            setPickedItems((prev) => {
              const next = { ...prev };
              delete next[cap];
              return next;
            });
          },
          onError: (err: Error) => {
            const msg = err.message || String(err);
            if (msg.includes("409") || msg.includes("already running")) {
              setError(`A probe for ${suggestion.vendor}:${suggestion.model} is already running — one model and one leaf at a time.`);
            } else {
              setError(msg);
            }
          },
        },
      );
      break;
    }
  };

  const estimateLabel = data.estimated_tokens.comparable
    ? `~${Math.round(data.estimated_tokens.tokens_per_attempt ?? 0).toLocaleString()} tokens per attempt (${data.estimated_tokens.reported} reported)`
    : data.estimated_tokens.reason ?? "no estimate";

  return (
    <section>
      <h2 className="text-[14px] font-semibold tracking-tight">Probe</h2>
      <p className="mb-3 mt-0.5 text-[12px] text-muted">
        Targeted re-runs on closed items with a red sabotage. One model and one leaf at a time,
        under the project&apos;s caps.
      </p>
      <div data-testid="harness-probe-panel" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-[13px] font-semibold">Candidates</span>
          <span className="font-mono text-[10.5px] text-faint" data-testid="harness-probe-estimate">
            {estimateLabel}
          </span>
        </div>

        {suggestions.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5" data-testid="harness-probe-suggestions">
            {suggestions.map((s, i) => {
              const active = pickedSuggestion === i;
              return (
                <button
                  key={`${s.vendor}:${s.model}:${s.binary_version}`}
                  type="button"
                  data-testid="harness-probe-suggestion"
                  onClick={() => { setPickedSuggestion(active ? null : i); setError(null); }}
                  className={`rounded-[6px] border px-2 py-1 font-mono text-[11px] transition-colors ${
                    active
                      ? "border-st-done bg-[rgba(80,200,120,0.12)] text-st-done"
                      : "border-line-2 bg-surface text-muted hover:border-line"
                  }`}
                >
                  {s.vendor}:{s.model}
                  {s.binary_version ? `:${s.binary_version}` : ""}
                  <span className="ml-1 text-faint">({s.trigger})</span>
                </button>
              );
            })}
          </div>
        )}

        {hasCandidates && (
          <div className="mt-3 flex flex-col gap-2.5" data-testid="harness-probe-candidates">
            {groups.map((group) => {
              const selected = pickedItems[group.key] ?? [];
              return (
                <div key={group.key} data-testid="harness-probe-group">
                  <div className="flex items-baseline gap-2 font-mono text-[11px]">
                    <span className="text-muted">{group.label}</span>
                    <span className="text-faint">{group.items.length} candidates</span>
                    {selected.length > 0 && (
                      <span className="text-st-done">{selected.length} picked</span>
                    )}
                  </div>
                  <div className="mt-1 flex flex-col gap-0.5">
                    {group.items.map((item) => {
                      const checked = selected.includes(item.id);
                      return (
                        <label
                          key={item.id}
                          data-testid="harness-probe-item"
                          className={`flex cursor-pointer items-start gap-2 rounded-[4px] px-2 py-1 font-mono text-[11px] transition-colors ${
                            checked ? "bg-[rgba(80,200,120,0.06)]" : "hover:bg-surface"
                          }`}
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={!checked && totalPicked >= 3}
                            onChange={() => toggleItem(group.key, item.id)}
                            className="mt-0.5 accent-st-done"
                          />
                          <span className="text-muted">{item.title}</span>
                        </label>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {error && (
          <div data-testid="harness-probe-error" className="mt-2 rounded-[6px] border border-[rgba(240,100,100,0.3)] bg-[rgba(240,100,100,0.08)] px-2.5 py-1.5 font-mono text-[11px] text-[#f06464]">
            {error}
          </div>
        )}

        {suggestion && totalPicked > 0 && (
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              data-testid="harness-probe-start"
              disabled={startRun.isPending}
              onClick={handleStart}
              className="inline-flex items-center gap-1.5 rounded-[6px] border border-st-done bg-[rgba(80,200,120,0.12)] px-3 py-1.5 font-mono text-[11.5px] text-st-done transition-colors hover:bg-[rgba(80,200,120,0.2)] disabled:opacity-50"
            >
              <Play size={12} />
              {startRun.isPending ? "Starting…" : `Start probe — ${suggestion.vendor}:${suggestion.model}`}
            </button>
            <span className="font-mono text-[10.5px] text-faint">
              {totalPicked} item{totalPicked === 1 ? "" : "s"} → {suggestion.trigger}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
