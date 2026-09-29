import { cn } from "@/lib/cn";
import type { ActivityHistogram } from "@/lib/types";

import { bucketRangeLabel, bucketTotal, bucketWidthLabel, coverageNote, SERIES } from "./model";

/**
 * 48 stacked bars over the selected range, clickable to filter (PRD-47 S10).
 *
 * Three absences are kept apart, because all three would otherwise render as a flat,
 * quiet-looking chart: no range asked for (`not_requested`), a window too big for one
 * read (`partial`, with the scanned count named), and a genuine zero in every bar.
 */
export function Histogram({
  histogram,
  selected,
  onPick,
}: {
  histogram: ActivityHistogram;
  selected: number | null;
  onPick: (index: number | null) => void;
}) {
  const note = coverageNote(histogram);
  const bars = histogram.buckets;
  const peak = bars.reduce((m, b) => Math.max(m, bucketTotal(b)), 0);

  return (
    <section className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="text-[12.5px] text-fg">
          {bars.length} × {bucketWidthLabel(histogram.bucket_seconds)} bars
        </h2>
        <Legend />
        {histogram.coverage === "full" && peak > 0 && (
          <span className="ml-auto text-[11px] text-faint">
            {bars.reduce((n, b) => n + bucketTotal(b), 0).toLocaleString()} events in range
          </span>
        )}
      </div>

      {bars.length === 0 ? (
        // `not_requested`: there is no window, so there is nothing to draw and no zero to
        // report. A flat chart here would be the absence reading as a quiet project.
        <p className="py-6 text-center text-[12.5px] leading-relaxed text-muted">{note}</p>
      ) : peak === 0 ? (
        <p className="py-6 text-center text-[12.5px] leading-relaxed text-muted">
          {note ??
            "Nothing recorded in this range. That is a true zero over the whole window, not a chart that failed to load."}
        </p>
      ) : (
        <>
          {/* `partial` keeps its bars: the caveat qualifies them, it does not replace
              them. Hiding the chart here would make the note describe bars nobody can
              see, and a window that is merely too big to scan in one read would look
              the same as a window with nothing in it. */}
          {note && <p className="mb-1.5 text-[11.5px] leading-snug text-st-review">{note}</p>}
          <div
            className="flex h-20 items-end gap-px"
            role="group"
            aria-label={`Events per ${bucketWidthLabel(histogram.bucket_seconds)} bar`}
          >
            {bars.map((b) => {
              const total = bucketTotal(b);
              const isSel = selected === b.index;
              return (
                <button
                  key={b.index}
                  type="button"
                  data-bucket={b.index}
                  aria-pressed={isSel}
                  aria-label={`${bucketRangeLabel(b)}: ${total} event${total === 1 ? "" : "s"}${
                    total ? `, ${SERIES.filter((s) => b[s.key] > 0).map((s) => `${s.label.toLowerCase()} ${b[s.key]}`).join(", ")}` : ""
                  }`}
                  title={`${bucketRangeLabel(b)} — ${total} event${total === 1 ? "" : "s"}`}
                  onClick={() => onPick(isSel ? null : b.index)}
                  className={cn(
                    "group flex h-full min-w-0 flex-1 flex-col justify-end rounded-[2px] transition-colors",
                    isSel ? "bg-fg/10 ring-1 ring-line-hover" : "hover:bg-fg/5",
                  )}
                >
                  {total === 0 ? (
                    <span className="h-px w-full bg-line-2" />
                  ) : (
                    SERIES.map((s) =>
                      b[s.key] > 0 ? (
                        <span
                          key={s.key}
                          className={cn("w-full", s.bar)}
                          style={{ height: `${(b[s.key] / peak) * 100}%` }}
                        />
                      ) : null,
                    )
                  )}
                </button>
              );
            })}
          </div>
        </>
      )}

      {bars.length > 0 && peak > 0 && (
        <div className="mt-1 flex justify-between text-[10.5px] text-faint">
          <span>{new Date(bars[0]!.start).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" })}</span>
          <span>oldest → newest</span>
          <span>{new Date(bars[bars.length - 1]!.end).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" })}</span>
        </div>
      )}
    </section>
  );
}

function Legend() {
  return (
    <ul className="flex flex-wrap items-center gap-x-3 gap-y-1">
      {SERIES.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5 text-[11px] text-muted">
          <span className={cn("h-2 w-2 rounded-[2px]", s.bar)} aria-hidden />
          {s.label}
        </li>
      ))}
    </ul>
  );
}
