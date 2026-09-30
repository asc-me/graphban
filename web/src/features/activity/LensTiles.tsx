import { cn } from "@/lib/cn";
import type { ActivityLens, ActivityLensId } from "@/lib/types";

/**
 * The six lenses (PRD-47 S10). A tile is a filter and a claim about coverage at the same
 * time, so the `rejected` tile names the refusal actions it reads: a count labelled
 * "rejected" that silently means three kinds of rejection is the absence-reads-as-clean
 * defect wearing a tile.
 */
export function LensTiles({
  lenses,
  active,
  onPick,
}: {
  lenses: ActivityLens[];
  active: ActivityLensId;
  onPick: (id: ActivityLensId) => void;
}) {
  return (
    <div
      className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6"
      role="group"
      aria-label="Activity lenses"
    >
      {lenses.map((lens) => (
        <LensTile key={lens.id} lens={lens} active={active === lens.id} onPick={() => onPick(lens.id)} />
      ))}
    </div>
  );
}

function LensTile({
  lens,
  active,
  onPick,
}: {
  lens: ActivityLens;
  active: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onPick}
      aria-pressed={active}
      title={lens.hint}
      className={cn(
        "flex flex-col gap-1 rounded-[10px] border px-3 py-2.5 text-left transition-colors",
        active
          ? "border-control-hover bg-surface-3"
          : "border-control bg-surface-2 hover:border-control-hover",
      )}
    >
      <span className="flex items-baseline justify-between gap-2">
        <span className={cn("text-body", active ? "text-fg" : "text-muted")}>{lens.label}</span>
        <span className="font-mono text-[15px] leading-none text-fg">{lens.count.toLocaleString()}</span>
      </span>
      {lens.covers ? (
        // Stated on the tile, not hidden in a tooltip: this lens sees three recorded
        // refusal kinds and nothing else, at 0 and at 100 alike.
        <span className="text-[10.5px] leading-snug text-faint">
          {lens.covers.length} recorded refusal kinds only
          <span className="mt-0.5 block font-mono text-meta text-faint-2">
            {lens.covers.join(" · ")}
          </span>
        </span>
      ) : (
        <span className="line-clamp-2 text-[10.5px] leading-snug text-faint">{lens.hint}</span>
      )}
    </button>
  );
}
