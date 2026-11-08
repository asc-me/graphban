import { useEffect, useState } from "react";
import { Check, ChevronRight, X } from "lucide-react";

import { useHarnessCards, useMarkHarnessCard } from "@/lib/queries";
import type { HarnessCard, HarnessCardCell } from "@/lib/types";

export type CardStateFilter = "all" | "new" | "changed" | "handled";

export interface CardFilter {
  /** Rules switched OFF. R5 starts off: it only reprioritises draft evidence and never changes a pick. */
  off: string[];
  /** "vendor:model", or "" for every harness. */
  harness: string;
  state: CardStateFilter;
}

export const DEFAULT_CARD_FILTER: CardFilter = { off: ["R5"], harness: "", state: "all" };
const STORAGE_KEY = "harness-card-filter";

function storageKey(projectId?: string) {
  return `${STORAGE_KEY}:${projectId ?? ""}`;
}

function loadFilter(projectId?: string): CardFilter {
  try {
    const raw = window.localStorage.getItem(storageKey(projectId));
    if (!raw) return DEFAULT_CARD_FILTER;
    const v = JSON.parse(raw);
    if (!v || !Array.isArray(v.off) || typeof v.harness !== "string") return DEFAULT_CARD_FILTER;
    const state = ["all", "new", "changed", "handled"].includes(v.state) ? v.state : "all";
    return { off: v.off.filter((r: unknown) => typeof r === "string"), harness: v.harness, state };
  } catch {
    return DEFAULT_CARD_FILTER;
  }
}

/** Per-viewer filter state, persisted in localStorage (guarded: storage may throw). */
export function useCardFilter(projectId?: string): [CardFilter, (f: CardFilter) => void] {
  const [filter, setFilter] = useState<CardFilter>(() => loadFilter(projectId));
  useEffect(() => {
    setFilter(loadFilter(projectId));
  }, [projectId]);
  const update = (f: CardFilter) => {
    setFilter(f);
    try {
      window.localStorage.setItem(storageKey(projectId), JSON.stringify(f));
    } catch {
      /* storage unavailable: the choice holds for this session only */
    }
  };
  return [filter, update];
}

function harnessNames(card: HarnessCard): string[] {
  return card.cells.map((c) => `${c.cell.vendor}${c.cell.model ? `:${c.cell.model}` : ""}`);
}

/**
 * The server drops cards you have already handled unless their evidence moved, so a card it
 * returns as previously accepted or dismissed is one carrying `previously` — its `state` is
 * "new" again. `state` is still honoured for a payload fetched with include_seen.
 */
function matchesState(card: HarnessCard, state: CardStateFilter): boolean {
  const handled = card.previously != null || card.state !== "new";
  if (state === "new") return !handled;
  if (state === "changed") return !!card.previously?.evidence_changed;
  if (state === "handled") return handled;
  return true;
}

export function filterCards(cards: HarnessCard[], f: CardFilter): HarnessCard[] {
  return cards.filter(
    (c) =>
      !f.off.includes(c.rule) &&
      (f.harness === "" || harnessNames(c).includes(f.harness)) &&
      matchesState(c, f.state),
  );
}

/** Cards the default filter shows — what the tab badge counts. */
export function defaultVisibleCount(cards: HarnessCard[]): number {
  return filterCards(cards, DEFAULT_CARD_FILTER).length;
}

export function describeFilter(f: CardFilter, rules: string[]): string {
  const on = rules.filter((r) => !f.off.includes(r));
  const parts = ["No"];
  if (on.length === 0) parts.push("cards — every rule is switched off");
  else parts.push(on.length === 1 ? `${on[0]} cards` : `${on.join("/")} cards`);
  if (f.harness) parts.push(`for ${f.harness}`);
  if (f.state !== "all")
    parts.push(
      { new: "that are new", changed: "whose evidence changed", handled: "previously accepted or dismissed" }[
        f.state
      ],
    );
  return parts.join(" ");
}

/**
 * PRD-38 D7 — recommended changes, as drafts.
 *
 * A card names the rule that produced it, the cells it fired on, the sibling cells it did NOT
 * fire on, and a replay of what the change would have done to resolutions that actually
 * happened. Accepting one does not apply it: R1 and R2 hand you evidence text for a commit,
 * R3 and R4 send you to the profile or policy screen. The button here records that you have
 * seen the card, so it stays quiet until its numbers move.
 */
export function Recommendations({
  projectId,
  filter: filterProp,
}: {
  projectId?: string;
  filter?: [CardFilter, (f: CardFilter) => void];
}) {
  const { data, isLoading } = useHarnessCards(projectId);
  const mark = useMarkHarnessCard(projectId);
  const own = useCardFilter(projectId);
  const [filter, setFilter] = filterProp ?? own;

  if (isLoading || !data) return null;
  if (data.cards.length === 0) {
    return (
      <div
        data-testid="harness-no-cards"
        className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-body text-muted"
      >
        No recommended changes. R1–R7 fire on cells above the {data.floor}-attempt
        floor; nothing in the last {data.window_days} days met one.
      </div>
    );
  }

  const ruleSet = Array.from(new Set([...data.cards.map((c) => c.rule)])).sort();
  const counts = Object.fromEntries(
    ruleSet.map((r) => [r, data.cards.filter((c) => c.rule === r).length]),
  );
  const harnesses = Array.from(new Set(data.cards.flatMap(harnessNames))).sort();
  const shown = filterCards(data.cards, filter);
  const toggle = (r: string) =>
    setFilter({
      ...filter,
      off: filter.off.includes(r) ? filter.off.filter((x) => x !== r) : [...filter.off, r],
    });

  return (
    <div className="flex flex-col gap-2">
      <div data-testid="harness-card-filters" className="flex flex-wrap items-center gap-2">
        {ruleSet.map((r) => {
          const on = !filter.off.includes(r);
          return (
            <button
              key={r}
              type="button"
              data-testid="harness-rule-chip"
              data-rule={r}
              aria-pressed={on}
              onClick={() => toggle(r)}
              className={`rounded-full border px-2 py-0.5 font-mono text-small ${
                on ? "border-accent text-fg-2" : "border-control text-faint"
              }`}
            >
              {r} <span data-testid="harness-rule-count">{counts[r]}</span>
            </button>
          );
        })}
        <select
          aria-label="Harness"
          data-testid="harness-filter-harness"
          value={filter.harness}
          onChange={(e) => setFilter({ ...filter, harness: e.target.value })}
          className="rounded-[8px] border border-control bg-surface px-1.5 py-0.5 text-[11.5px]"
        >
          <option value="">All harnesses</option>
          {harnesses.map((h) => (
            <option key={h} value={h}>
              {h}
            </option>
          ))}
        </select>
        <select
          aria-label="State"
          data-testid="harness-filter-state"
          value={filter.state}
          onChange={(e) => setFilter({ ...filter, state: e.target.value as CardStateFilter })}
          className="rounded-[8px] border border-control bg-surface px-1.5 py-0.5 text-[11.5px]"
        >
          <option value="all">Any state</option>
          <option value="new">New</option>
          <option value="changed">Evidence changed</option>
          <option value="handled">Accepted or dismissed</option>
        </select>
      </div>
      {shown.length === 0 ? (
        <div
          data-testid="harness-filter-empty"
          className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-body text-muted"
        >
          {describeFilter(filter, ruleSet)}.
        </div>
      ) : (
        <div className="flex flex-col gap-2" data-testid="harness-cards">
          {shown.map((card) => (
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
    </div>
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
          <p className="mt-0.5 text-body text-muted">{card.detail}</p>
          {Array.isArray(card.draft?.projects) && (card.draft.projects as string[]).length > 0 && (
            <p data-testid="harness-card-projects" className="mt-1 font-mono text-small text-faint">
              projects: {(card.draft.projects as string[]).join(", ")}
            </p>
          )}
          {card.draft?.probe === true && (
            <p data-testid="harness-card-probe" className="mt-1 font-mono text-small text-faint">
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
          {!(
            (card.replay?.moves?.length ?? 0) === 0 && (card.replay?.changed ?? 0) === 0
          ) && (
            <p data-testid="harness-card-replay" className="mt-1.5 font-mono text-small text-faint">
              {card.replay?.summary}
              {(card.replay?.moves?.length ?? 0) > 0 &&
                ` — ${card.replay.moves
                  .map((m) => `${m.count} from ${m.from} to ${m.to}`)
                  .join(", ")}`}
              {card.replay?.truncated && " (replay truncated)"}
            </p>
          )}
        </div>
        <div className="flex flex-none gap-1.5">
          <button
            type="button"
            aria-label={`Accept ${card.title}`}
            data-testid="harness-accept"
            onClick={() => onMark("accept")}
            className="inline-flex items-center gap-1 rounded-[8px] border border-control px-2 py-1 text-[11.5px] hover:bg-surface"
          >
            <Check size={12} /> Accept
          </button>
          <button
            type="button"
            aria-label={`Dismiss ${card.title}`}
            data-testid="harness-dismiss"
            onClick={() => onMark("dismiss")}
            className="inline-flex items-center gap-1 rounded-[8px] border border-control px-2 py-1 text-[11.5px] text-muted hover:bg-surface"
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
            className="rounded-[8px] border border-line-2 bg-surface px-2.5 py-2 font-mono text-small"
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
          <li key={i} className="font-mono text-small">
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
