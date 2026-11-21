import type { HandleCheck, ProfilePatch, ProjectPattern, User } from "@/lib/types";

/** The editable draft. `avatar2` is "" for a solid dot. */
export interface ProfileDraft {
  name: string;
  handle: string;
  initials: string;
  avatar: string;
  avatar2: string;
  pattern: ProjectPattern;
}

export function draftFrom(u: User): ProfileDraft {
  return {
    name: u.name,
    handle: u.handle,
    initials: u.initials,
    avatar: u.avatar,
    avatar2: u.avatar2 ?? "",
    pattern: u.avatar_pattern ?? "none",
  };
}

const HANDLE_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const RESERVED = ["admin", "root", "support", "graphban"];

/** Format check as you type; mirrors the server's `handles.validate`. Null is fine. */
export function handleFormatProblem(handle: string): string | null {
  if (RESERVED.includes(handle)) return `@${handle} is reserved`;
  if (handle.length < 3 || handle.length > 24) return "Handles are 3–24 characters";
  if (!HANDLE_RE.test(handle)) return "Lowercase letters, digits and single hyphens, none at either end";
  return null;
}

/** The server's answer to a handle check, in words. Null when the handle is free. */
export function handleCheckProblem(r: HandleCheck): string | null {
  if (r.available) return null;
  if (r.code === "handle_taken") return r.org ? `@${r.handle} is taken in ${r.org}` : `@${r.handle} is taken`;
  if (r.code === "handle_reserved") return `@${r.handle} is reserved`;
  return handleFormatProblem(r.handle) ?? `@${r.handle} can't be used`;
}

const LABELS: [keyof ProfileDraft, string][] = [
  ["name", "name"],
  ["handle", "handle"],
  ["initials", "letters"],
  ["avatar", "colour"],
  ["avatar2", "second colour"],
  ["pattern", "pattern"],
];

/** Names what changed, for the save bar. */
export function changedLabels(saved: ProfileDraft, draft: ProfileDraft): string[] {
  return LABELS.filter(([k]) => saved[k] !== draft[k]).map(([, l]) => l);
}

/** Only the changed fields; `avatar2: null` clears the ombre. */
export function patchFrom(saved: ProfileDraft, draft: ProfileDraft): ProfilePatch {
  const p: ProfilePatch = {};
  if (saved.name !== draft.name) p.name = draft.name.trim();
  if (saved.handle !== draft.handle) p.handle = draft.handle;
  if (saved.initials !== draft.initials) p.initials = draft.initials.trim();
  if (saved.avatar !== draft.avatar) p.avatar = draft.avatar;
  if (saved.avatar2 !== draft.avatar2) p.avatar2 = draft.avatar2 || null;
  if (saved.pattern !== draft.pattern) p.avatar_pattern = draft.pattern;
  return p;
}
