import { useState } from "react";
import { Check, ChevronRight, X } from "lucide-react";

import { useHarnessCards, useMarkHarnessCard } from "@/lib/queries";
import type { HarnessCard, HarnessCardCell } from "@/lib/types";

/**
 * PRD-38 D7 — recommended changes, as drafts.
 *
 * A card names the rule that produced it, the cells it fired on, the sibling cells it did NOT
 * fire on, and a replay of what the change would have done to resolutions that actually
 * happened. Accepting one does not apply it: R1 and R2 hand you evidence text for a commit,
 * R3 and R4 send you to the profile or policy screen. The button here records that you have
 * seen the card, so it stays quiet until its numbers move.
 */
export function Recommendations({ projectId }: { projectId?: string }) {
  const { data, isLoading } = useHarnessCards(projectId);
  const mark = useMarkHarnessCard(projectId);

  if (isLoading || !data) return null;
  if (data.cards.length === 0) {
    return (
      <div
        data-testid="harness-no-cards"
        className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
      >
        No recommended changes. The four rules fire on cells above the {data.floor}-attempt
        floor; nothing in the last {data.window_days} days met one.
      </div>
    );
  }

  return (
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
          {card.previously?.evidence_changed && (
            <p data-testid="harness-card-returned" className="mt-1 text-[12px] text-[#f0b450]">
              You {card.previously.state} this on{" "}
              {new Date(card.previously.at).toLocaleDateString()} — the evidence has changed
              since.
            </p>
          )}
          <p data-testid="harness-card-replay" className="mt-1.5 font-mono text-[11px] text-faint">
            {card.replay.summary}
            {card.replay.moves.length > 0 &&
              ` — ${card.replay.moves
                .map((m) => `${m.count} from ${m.from} to ${m.to}`)
                .join(", ")}`}
            {card.replay.truncated && " (replay truncated)"}
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
            {c.cell.model ? `:${c.cell.model}` : ""} · {c.cell.lane}/{c.cell.task_class}/
            {c.cell.size_band} · {c.signed_off}/{c.finished}
            {c.below_floor ? " (below the floor)" : ""}
          </li>
        ))}
      </ul>
    </div>
  );
}
