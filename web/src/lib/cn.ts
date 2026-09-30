import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * tailwind-merge has to be TOLD which of our `text-*` utilities are font sizes.
 *
 * `text-` is a shared namespace: `text-muted` is a colour, `text-body` is a size, and
 * tailwind-merge only knows the ones Tailwind ships. Left to itself it read every role in
 * our scale as a colour and dropped it when a real colour followed in the same `cn()` —
 * `cn("text-body", "text-fg")` returned `"text-fg"` alone, and the element fell back to the
 * inherited 16px. 27 call sites lost their size that way, in dialogs, menus, chat bubbles
 * and the login page: places a route-level screenshot never opens.
 *
 * Arbitrary values were never affected — `text-[12.5px]` is unambiguous — so this could only
 * appear once the scale actually gained consumers (GRPH-1004). Add a role to the scale and
 * it must be added here too, or it is silently dropped wherever `cn()` also sets a colour.
 */
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: ["micro", "meta", "small", "body", "lead", "title"] }],
    },
  },
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
