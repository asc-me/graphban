import { cn } from "@/lib/cn";
import type { ActivityFacet, ActivityFacets } from "@/lib/types";

export type FacetDim = "person" | "surface" | "object";

const GROUPS: { dim: FacetDim; label: string; title: string }[] = [
  {
    dim: "person",
    label: "Person",
    title:
      "Who the ledger recorded this against. For an agent action that is the API key, not the person behind it — the row's `via` line names the person.",
  },
  { dim: "surface", label: "Surface", title: "Where the mutation came in: mcp, rest, public, system." },
  {
    dim: "object",
    label: "Object",
    title:
      "The target_type column. Most MCP writes record none, so `untyped` is a real bucket and the largest one, not a missing value.",
  },
];

/** Person / surface / object facets (PRD-47 S10). Each group lists its own dimension
 *  unfiltered, so picking a value does not collapse the list to the one value picked. */
export function FacetBar({
  facets,
  selected,
  onToggle,
}: {
  facets: ActivityFacets;
  selected: Record<FacetDim, string | null>;
  onToggle: (dim: FacetDim, value: string) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      {GROUPS.map((g) => (
        <FacetGroup
          key={g.dim}
          label={g.label}
          title={g.title}
          facet={facets[g.dim]}
          selected={selected[g.dim]}
          onToggle={(v) => onToggle(g.dim, v)}
        />
      ))}
    </div>
  );
}

function FacetGroup({
  label,
  title,
  facet,
  selected,
  onToggle,
}: {
  label: string;
  title: string;
  facet: ActivityFacet;
  selected: string | null;
  onToggle: (value: string) => void;
}) {
  return (
    // A labelled group, not a bare run of buttons: the same value can legitimately appear
    // in a row AND in a facet, and without this the two are indistinguishable to a screen
    // reader (and to a test) — "alex" in the ledger and "alex" in the person facet are
    // different claims.
    <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label={`Filter by ${label.toLowerCase()}`}>
      <span className="w-14 flex-none text-[11.5px] text-faint" title={title}>
        {label}
      </span>
      {facet.values.length === 0 ? (
        // Distinct from "not measured": nothing in the current selection has a value here.
        <span className="text-[11.5px] text-faint-2">nothing in this selection</span>
      ) : (
        facet.values.map((v) => {
          const isSel = selected === v.value;
          return (
            <button
              key={`${v.kind ?? ""}:${v.value}`}
              type="button"
              aria-pressed={isSel}
              onClick={() => onToggle(v.value)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11.5px] transition-colors",
                isSel
                  ? "border-line-hover bg-surface-3 text-fg"
                  : "border-line-2 text-muted hover:border-line-hover hover:text-ink",
              )}
            >
              <span className="max-w-[14rem] truncate">{v.label}</span>
              <span className="font-mono text-[10px] text-faint">{v.count}</span>
            </button>
          );
        })
      )}
      {facet.truncated && (
        <span className="text-[10.5px] text-faint-2" title="The list is a top-N by count, not the whole set.">
          + more not listed
        </span>
      )}
    </div>
  );
}
