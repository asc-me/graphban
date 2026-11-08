import * as React from "react";

import { Button } from "@/components/ui/button";
import { MachineState } from "@/features/settings/MachinesPanel";
import { useMachines, useProjectSetup } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import { Link } from "react-router-dom";

/** Open the quick-start dialog at ③ (its paste-a-key route). The shell owns the dialog. */
const CONNECT_EVENT = "graphban:open-quickstart-connect";
export function requestConnectStep() {
  window.dispatchEvent(new Event(CONNECT_EVENT));
}
export function onConnectStepRequested(fn: () => void): () => void {
  window.addEventListener(CONNECT_EVENT, fn);
  return () => window.removeEventListener(CONNECT_EVENT, fn);
}

const CONNECT_STEP = 5;
const dismissKey = (projectId: string) => `al.setupStatus.dismissed.${projectId}`;

/**
 * Dashboard "Setup status" card (GRPH-1442). Actionable, never passive: an unproven step says
 * WHY (never reported, or reported with n FAIL and the failing lines) and what to run next.
 * Dismiss hides only this card, for good; Settings → Machines keeps the list.
 */
export function SetupStatusCard({ projectId }: { projectId: string }) {
  const setup = useProjectSetup(projectId);
  const machines = useMachines();
  const [dismissed, setDismissed] = React.useState(() => {
    try {
      return localStorage.getItem(dismissKey(projectId)) === "1";
    } catch {
      return false;
    }
  });
  const step = setup.data?.steps.find((s) => s.step === CONNECT_STEP);
  if (dismissed || !step) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(dismissKey(projectId), "1");
    } catch {
      /* private mode: hide for this visit */
    }
    setDismissed(true);
  };

  if (step.status === "done") {
    const how = step.proof === "machine_report" ? `${step.machine ?? "a machine"} reported clean` : "first call seen";
    return (
      <section data-testid="setup-status-card" data-state="verified" className="mb-4 rounded-lg border border-line p-4">
        <p className="text-[13px] font-semibold text-accent">✓ Agent connection verified — {how}</p>
      </section>
    );
  }

  const reported = step.reported;
  const machine = reported ? (machines.data ?? []).find((m) => (m.label || m.machine_id) === reported.machine) ?? null : null;
  return (
    <section data-testid="setup-status-card" data-state="unproven" className="mb-4 rounded-lg border border-line p-4">
      <h2 className="text-[13px] font-semibold">Setup status — Connect your agent</h2>
      {reported ? (
        <div className="mt-2" data-testid="setup-status-failed">
          <p className="text-[13px] text-danger">
            {reported.machine} reported with {reported.fails} FAIL
          </p>
          <ul className="mt-1 space-y-0.5 font-mono text-[12px] text-danger">
            {reported.failing.map((l, i) => (
              <li key={i}>FAIL {l.name}{l.detail ? ` — ${l.detail}` : ""}</li>
            ))}
          </ul>
          {machine ? <div className="mt-1"><MachineState machine={machine} /></div> : null}
        </div>
      ) : (
        <p className="mt-2 text-[13px] text-muted" data-testid="setup-status-never">
          <MachineState machine={null} /> — no machine has reported for this project yet.
        </p>
      )}
      <p className="mt-2 text-[13px] text-fg-2">
        Next: run <code>gban doctor</code> on that machine. On a dedicated server, also check it from
        outside: <code>curl -fsS &lt;your-server&gt;/health</code>.
      </p>
      <div className="mt-3 flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={requestConnectStep}>Verify with a key instead</Button>
        <Link className="text-[12px] text-muted underline" to={settingsPath("account/machines")}>Machines</Link>
        <Button variant="ghost" size="sm" onClick={dismiss}>Dismiss</Button>
      </div>
    </section>
  );
}
