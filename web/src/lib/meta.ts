import type { RequestType, Status } from "./types";

export const STATUS_META: Record<Status, { label: string; color: string }> = {
  backlog: { label: "Backlog", color: "#8b949e" },
  next: { label: "Next", color: "#7ca2ff" },
  in_progress: { label: "In Progress", color: "#c6f24e" },
  review: { label: "Review", color: "#e0b34a" },
  done: { label: "Done", color: "#5fd07a" },
  blocked: { label: "Blocked", color: "#ff6b6b" },
};

export const STATUS_ORDER: Status[] = [
  "backlog",
  "next",
  "in_progress",
  "review",
  "done",
  "blocked",
];

export const TYPE_META: Record<
  RequestType,
  { label: string; color: string; bg: string; border: string }
> = {
  bug: { label: "BUG", color: "#ff8f8f", bg: "rgba(255,107,107,0.08)", border: "rgba(255,107,107,0.22)" },
  feature: { label: "FEATURE", color: "#c6f24e", bg: "rgba(198,242,78,0.07)", border: "rgba(198,242,78,0.2)" },
  enhancement: { label: "ENHANCEMENT", color: "#7ca2ff", bg: "rgba(124,162,255,0.08)", border: "rgba(124,162,255,0.2)" },
  feedback: { label: "FEEDBACK", color: "#c9b8ff", bg: "rgba(167,139,250,0.08)", border: "rgba(167,139,250,0.2)" },
};

export const PR_STATE_COLOR: Record<string, string> = {
  open: "#5fd07a",
  merged: "#a78bfa",
  review: "#e0b34a",
  draft: "#8b949e",
};

export const CHECK_COLOR: Record<string, string> = {
  passing: "#5fd07a",
  pending: "#e0b34a",
  failing: "#ff6b6b",
};

/** Tracker board column order (PRD-P60 §D3) — Blocked sits beside Review, Done last. */
export const BOARD_COLUMNS: Status[] = [
  "backlog",
  "next",
  "in_progress",
  "review",
  "blocked",
  "done",
];
