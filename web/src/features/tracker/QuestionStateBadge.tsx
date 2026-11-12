import type { QuestionState } from "@/lib/types";

/** The server's `question_state` as words (GRPH-P75 D1). It replaces the plain blocked badge;
 * the web never derives the state itself. */
export const QUESTION_LABEL: Record<QuestionState, string> = {
  checking: "Checking context",
  response_needed: "Response Needed",
};

export function QuestionStateBadge({ state }: { state?: QuestionState | null }) {
  if (!state) return null;
  return (
    <span
      data-testid="question-state"
      className="flex-none rounded-md border border-st-blocked/40 bg-st-blocked/[0.08] px-1.5 py-0.5 font-mono text-[10px] text-st-blocked"
    >
      {QUESTION_LABEL[state]}
    </span>
  );
}
