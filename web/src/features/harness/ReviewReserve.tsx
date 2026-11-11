import type { HarnessCapacity } from "@/lib/types";

/** Headroom kept back so review is never starved by build. The server warning is shown verbatim. */
export function ReviewReserve({ reserve }: { reserve: HarnessCapacity["review_reserve"] }) {
  const healthy = !reserve.warning && reserve.only_unknown_for.length === 0 && reserve.empty_for.length === 0;
  return (
    <div data-testid="capacity-reserve" className="flex flex-col rounded-[12px] border border-line-2 bg-surface-2">
      <div className="flex flex-wrap items-baseline gap-2.5 border-b border-line-2/70 px-4 py-3">
        <h3 className="text-[13.5px] font-semibold">Review reserve</h3>
        <span className="text-[12px] text-muted">Kept back so review is never starved by build.</span>
      </div>
      <div className="flex flex-col gap-2 px-4 py-3">
        {reserve.warning && (
          <div
            data-testid="capacity-reserve-warning"
            role="status"
            className="flex flex-col gap-[3px] rounded-[8px] border border-[rgba(240,180,80,0.45)] bg-[rgba(240,180,80,0.05)] px-[11px] py-[9px]"
          >
            <span className="font-mono text-meta font-semibold tracking-[0.6px] text-[#f0b450]">△ SERVER WARNING</span>
            <span data-testid="capacity-reserve-warning-text" className="font-mono text-[11.5px] leading-[1.5] text-fg">{reserve.warning}</span>
          </div>
        )}
        {healthy && (
          <p data-testid="capacity-reserve-healthy" className="flex items-center gap-1.5 text-body text-fg-2">
            <span aria-hidden="true" className="text-st-done">●</span>
            Review reserve held by a measured entitlement
          </p>
        )}
        {(reserve.only_unknown_for.length > 0 || reserve.empty_for.length > 0) && (
          <div className="grid grid-cols-[130px_minmax(0,1fr)] gap-x-3 gap-y-2 font-mono text-[11.5px]">
            {reserve.only_unknown_for.length > 0 && (
              <>
                <span className="text-muted">only_unknown_for</span>
                <span className="flex flex-wrap gap-1.5">
                  {reserve.only_unknown_for.map((v) => (
                    <span key={v} data-testid="capacity-reserve-unknown" className="rounded-[5px] border border-dashed border-muted px-1.5 text-fg-2">○ {v}</span>
                  ))}
                </span>
              </>
            )}
            {reserve.empty_for.length > 0 && (
              <>
                <span className="text-muted">empty_for</span>
                <span className="flex flex-wrap gap-1.5">
                  {reserve.empty_for.map((v) => (
                    <span key={v} data-testid="capacity-reserve-empty" className="rounded-[5px] border border-[rgba(255,159,90,0.55)] px-1.5 text-[#ff9f5a]">◒ {v}</span>
                  ))}
                </span>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
