import type { GraphLink, Item } from "@/lib/types";

/**
 * Acceptance clauses the server would parse: a list item under an Acceptance/Tests
 * heading or label. A parenthetical qualifier and a trailing `.` are the same label
 * (`**Acceptance (D3):**`, `Acceptance (D3):`, `**Acceptance.**`). A sentence that
 * only contains the word is not one.
 */
const ACCEPTANCE_HEADING =
  /^(#{1,4}\s*)?(\*\*|__)?(tests|acceptance( criteria)?)(\s*\([^)\n]{1,60}\))?[:.]?(\*\*|__)?[:.]?\s*$/i;

export function hasAcceptance(description: string): boolean {
  const lines = description.split("\n");
  let inSection = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (ACCEPTANCE_HEADING.test(line)) {
      inSection = true;
      continue;
    }
    if (/^#{1,6}\s/.test(line)) {
      inSection = false;
      continue;
    }
    if (inSection && /^([-*]|\d+[.)])\s+\S/.test(line)) return true;
  }
  return false;
}

/** What an unready line item still needs. `description` undefined = unknown, so acceptance is not claimed. */
export function lineNeeds(item: Item, description?: string): string[] {
  if (!item.line_id) return [];
  const out: string[] = [];
  if (description !== undefined && !hasAcceptance(description)) out.push("needs acceptance");
  if (!(item.touchpoints ?? []).some((t) => String(t).trim())) out.push("needs a file");
  return out;
}

/** Fix → the items it blocks, and item → the fixes blocking it (open ones only). */
export function fixBlockMaps(items: Item[], links: GraphLink[]) {
  const byId = new Map(items.map((i) => [i.id, i]));
  const blocks = new Map<string, string[]>();
  const blockedBy = new Map<string, string[]>();
  for (const l of links) {
    if (l.type !== "dependency") continue;
    const fix = byId.get(l.b);
    const blocked = byId.get(l.a);
    if (!fix || !blocked || fix.kind !== "fix" || fix.status === "done") continue;
    blocks.set(fix.id, [...(blocks.get(fix.id) ?? []), blocked.id]);
    blockedBy.set(blocked.id, [...(blockedBy.get(blocked.id) ?? []), fix.id]);
  }
  return { blocks, blockedBy };
}

export interface LineInfo {
  needs: string[];
  blockedBy: string[];
  blocks: string[];
}

const CHIP = "flex-none rounded-md border px-1.5 py-px font-mono text-[9px] uppercase tracking-wide";

/** SECURITY: amber mono, the PROTO chip's size (GRPH-1744). */
export function SecurityChip() {
  return (
    <span
      className="flex-none whitespace-nowrap rounded-md border border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] px-1.5 py-0.5 font-mono text-[9px] tracking-[0.5px] text-[#e0b34a]"
      data-testid="badge-security"
    >
      SECURITY
    </span>
  );
}

/**
 * What a line item still needs, as one quiet line — not a chip, and not a column: the item stays
 * in its status column (GRPH-1744).
 */
export function NeedsLine({ needs, className }: { needs?: string[]; className?: string }) {
  if (!needs || needs.length === 0) return null;
  return (
    <span className={`text-[11.5px] text-muted ${className ?? ""}`} data-testid="line-needs">
      {needs.join(" · ")}
    </span>
  );
}

/** Chips beside a line or fix item. Readiness is not one of them: see NeedsLine. */
export function LineBadges({
  item,
  blockedBy = [],
  blocks = [],
}: {
  item: Item;
  needs?: string[];
  blockedBy?: string[];
  blocks?: string[];
}) {
  return (
    <>
      {item.kind === "fix" && (
        <span className={`${CHIP} border-line-2 text-muted`} data-testid="badge-fix">
          FIX
        </span>
      )}
      {item.severity === "security" && <SecurityChip />}
      {item.status === "blocked" && blockedBy.length > 0 && (
        <span className={`${CHIP} border-[rgba(255,107,107,0.3)] text-st-blocked`} data-testid="badge-blocked-by">
          BLOCKED <span className="normal-case">by {blockedBy.join(", ")}</span>
        </span>
      )}
      {blocks.length > 0 && (
        <span className={`${CHIP} border-line-2 normal-case text-muted`} data-testid="badge-blocks">
          blocks {blocks.join(", ")}
        </span>
      )}
    </>
  );
}
