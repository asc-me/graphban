import type { HarnessCapacity } from "@/lib/types";

/** Headroom kept back so review is never starved by build. The server warning is shown verbatim. */
export function ReviewReserve({ reserve }: { reserve: HarnessCapacity["review_reserve"] }) {
  const healthy = !reserve.warning && reserve.only_unknown_for.length === 0 && reserve.empty_for.length === 0;
  return (
    <div data-testid="capacity-reserve">
      <div className="text-[13px] font-medium">Review reserve</div>
      <p className="text-body text-muted">Headroom kept back so review is never starved by build.</p>
      {reserve.warning && (
        <p data-testid="capacity-reserve-warning" role="alert" className="mt-1 text-body text-danger">
          <span className="font-mono text-small">△ SERVER WARNING</span>{" "}
          <span data-testid="capacity-reserve-warning-text">{reserve.warning}</span>
        </p>
      )}
      {healthy && (
        <p data-testid="capacity-reserve-healthy" className="mt-1 font-mono text-small">
          ● Review reserve held by a measured entitlement
        </p>
      )}
      {reserve.only_unknown_for.map((v) => (
        <div key={`u-${v}`} data-testid="capacity-reserve-unknown" className="font-mono text-small">○ {v}</div>
      ))}
      {reserve.empty_for.map((v) => (
        <div key={`e-${v}`} data-testid="capacity-reserve-empty" className="font-mono text-small">◒ {v}</div>
      ))}
    </div>
  );
}
