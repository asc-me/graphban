import { api } from "@/lib/api";
import type { TriageFeedRow, TriageTab } from "@/lib/types";

/** Reverses one action. Resolves once the server has restored the row. */
export type Undo = () => Promise<unknown>;

export interface TriageAction {
  /** Single lowercase letter; the chip shows it upper-cased. */
  key: string;
  label: string;
  /** Runs on ↵ as well as on its own key. */
  primary?: boolean;
  /** Override and Merge only: the actions Undo does not reverse cleanly ask first. */
  confirm?: string;
  run: () => Promise<{ result: string; undo: Undo }>;
}

export const TABS: { id: TriageTab; label: string; key: string; dot: string | null }[] = [
  { id: "all", label: "All", key: "1", dot: null },
  { id: "bugs", label: "Bugs", key: "2", dot: "#ff8f8f" },
  { id: "requests", label: "Requests", key: "3", dot: "#c6f24e" },
  { id: "blockers", label: "Blockers", key: "4", dot: "#ff6b6b" },
  { id: "collisions", label: "Collisions", key: "5", dot: "#e0b34a" },
];

/** Same split the server counts with (`services/triage.py:_tab_of`); feedback is in All only. */
export function inTab(row: TriageFeedRow, tab: TriageTab): boolean {
  if (tab === "all") return true;
  if (tab === "bugs") return row.kind === "bug";
  if (tab === "requests") return row.kind === "feature" || row.kind === "enhancement";
  if (tab === "blockers") return row.kind === "blocker";
  return row.kind === "collision";
}

export function formatAge(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

const iso = (ms: number) => new Date(Date.now() + ms).toISOString();
const HOUR = 3_600_000;

/** Letters for a decision wait's options. `e` and `s` are Escalate and Snooze. */
const OPTION_KEYS = "abcdfg";

export function actionsFor(row: TriageFeedRow, projectId: string): TriageAction[] {
  if (row.kind === "blocker") {
    const owner = row.owner ?? "the agent";
    const escalate: TriageAction = {
      key: "e", label: "Escalate",
      run: async () => {
        await api.blockerAction(row.id, "escalate");
        return { result: "Escalated to project owner", undo: () => api.undoBlocker(row.id) };
      },
    };
    const snooze: TriageAction = {
      key: "s", label: "Snooze 1d",
      run: async () => {
        await api.snoozeBlocker(row.id, iso(24 * HOUR));
        return { result: "Snoozed until tomorrow", undo: () => api.undoBlocker(row.id) };
      },
    };
    if (row.decision) {
      // No option is primary: ↵ must not answer a human decision on the agent's behalf.
      const options = row.decision.options.slice(0, OPTION_KEYS.length).map<TriageAction>((o, i) => ({
        key: OPTION_KEYS[i], label: o,
        run: async () => {
          await api.answerBlocker(row.id, o);
          return { result: `Answered "${o}" — ${owner} resumed`, undo: () => api.undoBlocker(row.id) };
        },
      }));
      return [...options, escalate, snooze];
    }
    return [
      {
        key: "u", label: "Mark unblocked", primary: true,
        run: async () => {
          await api.blockerAction(row.id, "unblock");
          return { result: `Unblocked — ${owner} resumes`, undo: () => api.undoBlocker(row.id) };
        },
      },
      escalate,
      {
        key: "l", label: "Release claim",
        run: async () => {
          await api.blockerAction(row.id, "release-claim");
          return { result: "Claim released — back to Next", undo: () => api.undoBlocker(row.id) };
        },
      },
      snooze,
    ];
  }

  if (row.kind === "collision") {
    const ids = row.members.map((m) => m.id);
    const decide = (
      decision: "serialize" | "parallel" | "hold" | "override" | "snooze",
      result: string,
      until?: string,
    ) => async () => {
      const d = await api.decideCollision(projectId, ids, decision, until);
      return { result, undo: () => api.undoCollisionDecision(d.id) };
    };
    const snooze: TriageAction = {
      key: "s", label: "Snooze 1h", run: async () => decide("snooze", "Snoozed 1h", iso(HOUR))(),
    };
    if (row.risk === "blocked") {
      return [
        { key: "h", label: `Hold ${ids[1]}`, primary: true,
          run: decide("hold", `Holding ${ids.slice(1).join(", ")} until ${ids[0]} merges`) },
        { key: "o", label: "Override", confirm: "Override allows overlapping claims on this code. Project owner only.",
          run: decide("override", "Override — both claims allowed") },
        snooze,
      ];
    }
    return [
      { key: "q", label: "Serialize", primary: true,
        run: decide("serialize", `Serialized — ${ids.slice(1).join(", ")} queued behind ${ids[0]}`) },
      { key: "p", label: "Allow parallel", run: decide("parallel", "Marked safe to parallelize") },
      snooze,
    ];
  }

  // A request. Undo is by the action id the server returned.
  const undoBy = (actionId: string | null): Undo => {
    // Lazy: the action already landed, so a missing id must surface at Undo, not as a failed action.
    return () =>
      actionId
        ? api.undoRequestAction(row.id, actionId)
        : Promise.reject(new Error("the server returned no action id to undo with"));
  };
  const accept = (status: "backlog" | "next", label: string, key: string, primary: boolean): TriageAction => ({
    key, label, primary,
    run: async () => {
      const r = await api.acceptRequestTo(row.id, status, row.suggested_prd?.id);
      const linked = row.suggested_prd ? ` · linked ${row.suggested_prd.id}` : "";
      return {
        result: `Accepted → ${status === "next" ? "Next" : "Backlog"} as ${r.item.id}${linked}`,
        undo: undoBy(r.action_id),
      };
    },
  });
  const rest: TriageAction[] = [
    accept("backlog", row.duplicate ? "Accept as new" : "Accept to Backlog", "a", !row.duplicate),
    accept("next", "Accept to Next", "n", false),
    {
      key: "d", label: "Decline",
      run: async () => {
        const r = await api.declineRequest(row.id);
        return { result: "Declined — submitter notified", undo: undoBy(r.action_id) };
      },
    },
    {
      key: "s", label: "Snooze 1w",
      run: async () => {
        const r = await api.snoozeRequest(row.id, iso(7 * 24 * HOUR));
        return { result: "Snoozed 1 week", undo: undoBy(r.action_id) };
      },
    },
  ];
  if (!row.duplicate) return rest;
  const dup = row.duplicate;
  return [
    {
      key: "m", label: `Merge into ${dup.id}`, primary: true,
      confirm: `Merge into ${dup.id}? Its votes carry over and this report closes.`,
      run: async () => {
        const r = await api.mergeRequest(row.id, dup.id);
        return { result: `Merged into ${dup.id} (+${row.votes} votes)`, undo: undoBy(r.action_id) };
      },
    },
    ...rest,
  ];
}
