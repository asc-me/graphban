import { useEvents } from "@/lib/queries";

import { RefusalRuleLink } from "./RefusalRuleLink";
import { relTime } from "./model";

/**
 * The events recorded against one PRD or item (PRD-P61 §8, frames 4a1/4b1). A refusal row
 * carries its "see rule" (or the unmapped copy and "see all rules") right on the row.
 */
export function TargetActivity({
  projectId,
  targetType,
  targetId,
}: {
  projectId: string;
  targetType: "prd" | "item";
  targetId: string;
}) {
  const { data, isLoading, isError } = useEvents({ projectId, targetType, targetId, limit: 50 });
  return (
    <section data-testid="target-activity" className="mt-5">
      <h3 className="mb-1.5 text-[11.5px] text-muted">Activity</h3>
      {isLoading ? (
        <p className="text-small text-faint">Loading activity…</p>
      ) : isError || !data ? (
        <p className="text-small text-st-blocked">Could not read this {targetType}'s activity.</p>
      ) : data.results.length === 0 ? (
        <p className="text-small text-faint">Nothing recorded against this {targetType} yet.</p>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {data.results.map((e) => {
            const isRefusal = e.action === "refusal" || e.action === "refusal_unmapped";
            const ruleId = typeof e.meta?.rule_id === "string" && e.meta.rule_id ? e.meta.rule_id : null;
            return (
              <li
                key={e.id}
                data-testid="target-activity-row"
                className="flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-[10px] border border-control bg-surface-2 px-3 py-2 text-[12px]"
              >
                <span className="font-mono text-[11.5px] text-accent">{e.action}</span>
                <span className="text-faint">{e.actor_label || e.actor_id}</span>
                <span className="ml-auto font-mono text-[10.5px] text-faint">{relTime(e.ts) || "no ts"}</span>
                {isRefusal && (
                  <div className="basis-full">
                    <RefusalRuleLink
                      ruleId={e.action === "refusal" ? ruleId : null}
                      eventId={e.id}
                      testId="refusal-rule"
                    />
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
