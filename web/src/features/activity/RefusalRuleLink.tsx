import { Link, useLocation } from "react-router-dom";

import { projectPath, tagFromPath } from "@/lib/routes";

/**
 * "see rule" for a refusal that names a rule; the unmapped sentence plus "see all rules" for
 * one that does not (GRPH-1164 / GRPH-1199). Shared by the event panel, the per-target
 * activity lists and the inline API refusal banners, so none of them can drift into a
 * refusal with prose and no way to the rule.
 */
export function RefusalRuleLink({
  ruleId,
  eventId,
  testId,
}: {
  ruleId: string | null;
  eventId?: number;
  testId?: string;
}) {
  const { pathname } = useLocation();
  const tag = tagFromPath(pathname);
  const base = tag ? projectPath(tag, "rules") : "/rules";
  if (ruleId) {
    const from = eventId === undefined ? "" : `&from=${eventId}`;
    return (
      <span className="inline-flex items-center gap-1.5" data-testid={testId}>
        <span className="font-mono text-[12px] text-fg">{ruleId}</span>
        <Link
          to={`${base}?rule=${encodeURIComponent(ruleId)}${from}`}
          className="text-small text-accent hover:underline"
        >
          see rule
        </Link>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5" data-testid={testId}>
      <span className="text-[12px] leading-snug text-muted">This refusal isn't mapped to a rule yet.</span>
      <Link to={`${base}?pulse=unmapped`} className="text-small text-accent hover:underline">
        see all rules
      </Link>
    </span>
  );
}
