import type { PrdStatus } from "@/lib/types";

export const PRD_STATUS_META: Record<PrdStatus, { label: string; color: string }> = {
  draft: { label: "Draft", color: "#8b949e" },
  review: { label: "Review", color: "#e0b34a" },
  approved: { label: "Approved", color: "#5fd07a" },
  // Reached by closing out a PRD, never picked — so it renders but is not settable, the same
  // arrangement `approved` already has.
  closed: { label: "Closed", color: "#8b6fd0" },
};

export const PRD_STATUS_ORDER: PrdStatus[] = ["draft", "review", "approved", "closed"];

/**
 * The meta for a status, INCLUDING one this build has never heard of.
 *
 * `PRD_STATUS_META[p.status]` was indexed directly, and when the backend gained `closed`
 * the lookup returned undefined, `meta.color` threw, and one row with an unmapped status
 * took down the entire PRD list and editor — a white screen, on every route that renders a
 * PRD (GRPH-458). Nothing else in the app renders a PRD status, which is why every other
 * view kept working and the fault looked like "the PRD page is broken".
 *
 * A status the server invents tomorrow must cost one odd-looking chip, not the page. The
 * label is the raw value rather than "Unknown", because the person looking at it needs to
 * know WHICH status their build cannot name in order to report it.
 */
export function prdStatusMeta(status: string): { label: string; color: string } {
  return PRD_STATUS_META[status as PrdStatus] ?? { label: status, color: "#8b949e" };
}

/** What a person may actually choose (PRD-15). `approved` stays in PRD_STATUS_ORDER —
 *  it is still a real status that has to render — but it is REACHED by finishing the
 *  grill, never picked, so it is absent here. */
export const PRD_SETTABLE_STATUSES: PrdStatus[] = ["draft", "review"];
