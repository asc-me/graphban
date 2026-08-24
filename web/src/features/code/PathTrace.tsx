import { cn } from "@/lib/cn";
import type { CodePath } from "@/lib/types";

/**
 * The shortest route between two nodes — PRD-20 AC-19 (GRPH-481).
 *
 * **Walked undirected, reported directed.** The route ignores arrow direction, because "how are
 * these two related" is not a question about import order; but every hop then says which way its
 * edge actually points, because that is the half that tells you what depends on what. A trace
 * that hid direction would answer the easy question and look like it answered the hard one.
 *
 * **`found: false` and "no such node" are different answers** and are shown differently. An
 * endpoint the graph has never heard of is a coverage gap — this graph describes a minority of
 * the tree — while two known nodes with no route between them is a real structural fact. Collapsing
 * them into one "not found" would report a missing description as an architectural boundary.
 */
interface Props {
  path: CodePath;
  onClear: () => void;
  onPick: (path: string) => void;
  /** Named so "no route" can say whether it was scoped to a subset of the edges. */
  edgeTypes: string[];
  allEdgeTypes: string[];
}

const leaf = (p: string) => {
  const i = p.lastIndexOf("/");
  return { dir: i >= 0 ? p.slice(0, i + 1) : "", name: i >= 0 ? p.slice(i + 1) : p };
};

function Node({ path, onPick }: { path: string; onPick: (p: string) => void }) {
  const { dir, name } = leaf(path);
  return (
    <button
      type="button"
      onClick={() => onPick(path)}
      className="block w-full truncate text-left font-mono text-[11.5px] hover:underline"
    >
      <span className="text-faint">{dir}</span>
      <span className="text-fg-2">{name}</span>
    </button>
  );
}

export function PathTrace({ path, onClear, onPick, edgeTypes, allEdgeTypes }: Props) {
  const scoped = edgeTypes.length > 0 && edgeTypes.length < allEdgeTypes.length;

  return (
    <div className="w-[352px] overflow-hidden rounded-xl border border-line-2 bg-surface-2">
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2.5">
        <h2 className="text-[13px] font-semibold tracking-[-0.1px]">
          Path
          {path.found && (
            <span className="ml-2 font-mono text-[11px] font-normal text-muted">
              {path.hops.length} {path.hops.length === 1 ? "hop" : "hops"}
            </span>
          )}
        </h2>
        <button
          type="button"
          onClick={onClear}
          aria-label="Clear the path"
          className="px-1 text-[15px] leading-none text-faint hover:text-fg-2"
        >
          &times;
        </button>
      </div>

      {path.missing.length > 0 ? (
        <div className="px-3.5 py-4 text-[11.5px] leading-relaxed text-muted">
          {/* Not "no path": the graph has never heard of these, which is a coverage gap and not
              a fact about the architecture. */}
          <p className="mb-1 text-fg-2">Not on the map.</p>
          {path.missing.map((m) => (
            <p key={m} className="truncate font-mono text-[11px] text-faint">{m}</p>
          ))}
          <p className="mt-2 text-faint">
            Nothing has described {path.missing.length === 1 ? "it" : "them"} yet, so there is no
            route to look for.
          </p>
        </div>
      ) : !path.found ? (
        <div className="px-3.5 py-4 text-[11.5px] leading-relaxed text-muted">
          <p className="mb-1 text-fg-2">No route between them.</p>
          <p className="text-faint">
            {scoped
              ? `Both are on the map, but nothing connects them through ${edgeTypes.join(" + ")} edges — try switching a chip back on.`
              : "Both are on the map, and nothing connects them. They are in different components."}
          </p>
        </div>
      ) : (
        <ol className="max-h-[300px] overflow-y-auto px-3.5 py-3">
          <li className="min-w-0">
            <Node path={path.a} onPick={onPick} />
          </li>
          {path.hops.map((h, i) => (
            <li key={`${h.src}->${h.dst}-${i}`} className="min-w-0">
              <div className="flex items-center gap-2 py-1.5 pl-1">
                {/* Down = the edge points the way you walked it. Up = you walked it against
                    the arrow. This is the whole of "which way each edge actually points". */}
                <span
                  aria-hidden
                  className={cn(
                    "font-mono text-[13px] leading-none",
                    h.forward ? "text-accent" : "text-purple",
                  )}
                >
                  {h.forward ? "↓" : "↑"}
                </span>
                <span className="font-mono text-[10.5px] text-muted">{h.type}</span>
                <span className="text-[10.5px] text-faint">
                  {h.forward ? "points this way" : "points back"}
                </span>
              </div>
              <Node path={h.dst} onPick={onPick} />
            </li>
          ))}
        </ol>
      )}

      <div className="border-t border-line px-3.5 pb-3 pt-2 text-[10.5px] leading-relaxed text-faint">
        Walked without regard to direction; each hop reports which way its edge points
        {scoped ? `, over ${edgeTypes.join(" + ")} edges` : ""}.
      </div>
    </div>
  );
}
