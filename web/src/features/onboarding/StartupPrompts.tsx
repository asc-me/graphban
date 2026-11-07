import * as React from "react";

import { Button } from "@/components/ui/button";

/** The four start-up prompts → skills (GRPH-P67 D10; `Quick Start.dc.html`, Finish). */
export const STARTUP_PROMPTS: { label: string; skills: string[] }[] = [
  { label: "orient", skills: ["/graphban-orient"] },
  { label: "claim next", skills: ["/graphban-work"] },
  { label: "review", skills: ["/graphban-review"] },
  { label: "draft + grill a PRD", skills: ["/graphban-prd", "/graphban-grill"] },
];

/** The rows, shared by the Finish screen, the dashboard widget and Help. */
export function StartupPromptRows() {
  const [copied, setCopied] = React.useState<number | null>(null);
  return (
    <ul className="divide-y divide-line rounded-[8px] border border-line bg-bg" aria-label="Start-up prompts">
      {STARTUP_PROMPTS.map((p, i) => (
        <li key={p.label} className="flex items-center gap-3 px-3 py-2">
          <span className="w-[130px] flex-none text-body text-muted">{p.label}</span>
          <code className="min-w-0 flex-1 whitespace-pre-wrap font-mono text-[12px] text-fg">{p.skills.join("\n")}</code>
          <Button
            variant="outline"
            size="sm"
            type="button"
            aria-label={`Copy ${p.skills.join(" and ")}`}
            onClick={() => {
              void navigator.clipboard?.writeText(p.skills.join("\n")).catch(() => undefined);
              setCopied(i);
              setTimeout(() => setCopied((c) => (c === i ? null : c)), 1500);
            }}
          >
            <span aria-live="polite">{copied === i ? "Copied" : "Copy"}</span>
          </Button>
        </li>
      ))}
    </ul>
  );
}

const DISMISS_KEY = "graphban.startup-prompts-dismissed";

/** Per-browser, like the other dashboard preferences: dismissing is not a server fact. */
export function useStartupPromptsDismissed(): [boolean, () => void] {
  const [dismissed, setDismissed] = React.useState(() => {
    try {
      return localStorage.getItem(DISMISS_KEY) === "1";
    } catch {
      return false;
    }
  });
  return [
    dismissed,
    () => {
      try {
        localStorage.setItem(DISMISS_KEY, "1");
      } catch {
        /* storage blocked: dismiss for this visit only */
      }
      setDismissed(true);
    },
  ];
}

/** The dismissible dashboard widget. */
export function StartupPromptsWidget() {
  const [dismissed, dismiss] = useStartupPromptsDismissed();
  if (dismissed) return null;
  return (
    <section aria-labelledby="startup-prompts-h" className="mt-4 rounded-[11px] border border-line bg-surface-2 p-4" data-startup-prompts>
      <div className="mb-2 flex items-center gap-2">
        <h2 id="startup-prompts-h" className="flex-1 text-lead font-semibold">
          Start-up prompts
        </h2>
        <Button variant="ghost" size="sm" type="button" onClick={dismiss}>
          Dismiss
        </Button>
      </div>
      <StartupPromptRows />
    </section>
  );
}
