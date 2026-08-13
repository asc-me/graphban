/**
 * The roles a wave provisions (PRD-19).
 *
 * This file used to build a whole `~/.cursor/mcp.json` per wave, because a role could only
 * live in a credential and Cursor stores exactly one. **Enrolment seats replaced that**: the
 * credential is written once and never again, and the role arrives as a code pasted into the
 * agent's prompt. The config generator went with it rather than being left reachable-but-dead
 * — a passing test for unreachable code reads as coverage and is the opposite.
 *
 * `all-in-one` is deliberately absent: a wave is the fleet shape, and an un-enrolled agent
 * already gets the single-agent posture without needing a seat for it.
 */
export const WAVE_ROLES = ["planner", "worker", "reviewer"] as const;

export type WaveRole = (typeof WAVE_ROLES)[number];
