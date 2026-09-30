import { describe, expect, it } from "vitest";

import { cn } from "@/lib/cn";

import { TYPE_ROLES } from "./typeScale";

describe("cn keeps a type role alongside a colour", () => {
  // `text-` is a shared namespace, and tailwind-merge only knows the utilities Tailwind
  // ships. Until lib/cn.ts declared them, every role in our scale was read as a COLOUR and
  // dropped when a real colour followed — `cn("text-body", "text-fg")` returned `text-fg`
  // alone and the element fell back to the inherited 16px, in dialogs, menus, chat bubbles
  // and the login page. The full web suite was green throughout (GRPH-1004 review bounce).
  //
  // Driven off TYPE_ROLES on purpose: adding a seventh role without registering it in
  // lib/cn.ts fails here rather than silently losing the size at every cn() call site.
  it.each(TYPE_ROLES)("keeps %s when a colour follows", (role) => {
    const out = cn(role, "text-muted").split(" ");
    expect(out).toContain(role);
    expect(out).toContain("text-muted");
  });

  it("still collapses two colours to the last one", () => {
    expect(cn("text-muted", "text-fg")).toBe("text-fg");
  });

  // The other half of the same coin: teaching tailwind-merge the sizes must not stop it
  // de-duplicating them. A fix that simply passed everything through would satisfy the
  // assertions above and break every `cn(base, override)` in the codebase.
  it("still collapses two sizes to the last one", () => {
    expect(cn("text-body", "text-title")).toBe("text-title");
  });
});
