/** The type scale's roles, smallest first — one list, so the tests that assert the sizes and
 *  the tests that assert someone uses them cannot drift apart.
 *
 *  A plain module, not an export from a `.test.ts`: importing a test file runs its `describe`
 *  blocks in the importer too, which silently duplicated the whole token suite (GRPH-1004).
 */
export const TYPE_ROLES = [
  "text-micro",
  "text-meta",
  "text-small",
  "text-body",
  "text-lead",
  "text-title",
] as const;
