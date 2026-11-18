import type { GraphLink, Item } from "@/lib/types";

/** Acceptance clauses the server would parse: a list item under an Acceptance/Tests heading or label. */
export function hasAcceptance(description: string): boolean {
  const lines = description.split("\n");
  let inSection = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (/^(#{1,4}\s*)?(\*\*|__)?(tests|acceptance( criteria)?)(:)?(\*\*|__)?:?\s*$/i.test(line)) {
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

export function LineBadges({
  item,
  needs = [],
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
      {item.severity === "security" && (
        <span className={`${CHIP} border-[rgba(255,107,107,0.4)] text-st-blocked`} data-testid="badge-security">
          SECURITY
        </span>
      )}
      {needs.length > 0 && (
        <span className={`${CHIP} border-[#3a2f1a] normal-case text-[#e0b34a]`} data-testid="badge-needs">
          {needs.join(" · ")}
        </span>
      )}
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
