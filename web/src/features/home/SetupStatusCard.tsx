import * as React from "react";

import { Button } from "@/components/ui/button";
import { MachineState } from "@/features/settings/MachinesPanel";
import { useProjectSetup } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import type { SetupMachineReport, SetupStep } from "@/lib/types";
import { settingsPath } from "@/lib/routes";
import { countFrom, deriveMarks } from "@/features/onboarding/quickStartProgress";
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
 * WHY (never reported, or reported with n FAIL / UNKNOWN and those lines), one row per machine,
 * and what to run next.
 * Dismiss hides only this card, for good; Settings → Machines keeps the list.
 */
export function SetupStatusCard({ projectId }: { projectId: string }) {
  const setup = useProjectSetup(projectId);
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

  // ④ First job: the finish screen listed it as unproven, so the card keeps listing it, with the
  // same three counters the dialog reads, until it is ✓.
  const job = deriveMarks({ projectId, setup: setup.data }).first_job;
  const jobBlock = job.status === "done" ? null : (
    <FirstJobBlock mark={job} steps={setup.data?.steps ?? []} />
  );

  if (step.status === "done") {
    const how = step.proof === "machine_report" ? `${step.machine ?? "a machine"} reported clean` : "first call seen";
    return (
      <section data-testid="setup-status-card" data-state="verified" className="mb-4 rounded-lg border border-line p-4">
        <p className="text-[13px] font-semibold text-accent">✓ Agent connection verified — {how}</p>
        {jobBlock}
        {jobBlock ? (
          <div className="mt-3"><Button variant="ghost" size="sm" onClick={dismiss}>Dismiss</Button></div>
        ) : null}
      </section>
    );
  }

  // Each machine separately (§6): the browser cannot know which one you are sitting at.
  const rows: SetupMachineReport[] = step.machines?.length
    ? step.machines
    : step.reported
      ? [{ id: "", machine: step.reported.machine, state: step.reported.fails ? "failed" : "unknown",
           fails: step.reported.fails, unknowns: step.reported.unknowns ?? 0,
           failing: step.reported.failing, at: step.reported.at }]
      : [];
  return (
    <section data-testid="setup-status-card" data-state="unproven" className="mb-4 rounded-lg border border-line p-4">
      <h2 className="text-[13px] font-semibold">Setup status — Connect your agent{jobBlock ? " · First job" : ""}</h2>
      {rows.length ? (
        <ul className="mt-2 space-y-2">
          {rows.map((r, i) => <MachineReportRow key={r.id || i} row={r} />)}
        </ul>
      ) : (
        <p className="mt-2 text-[13px] text-muted" data-testid="setup-status-never">
          <MachineState machine={null} /> — this machine never reported for this project.
        </p>
      )}
      <p className="mt-2 text-[13px] text-fg-2">
        Next: run <code>gban doctor</code> on that machine. On a dedicated server, also check it from
        outside: <code>curl -fsS &lt;your-server&gt;/health</code>.
      </p>
      {jobBlock}
      <div className="mt-3 flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={requestConnectStep}>Verify with a key instead</Button>
        <Link className="text-[12px] text-muted underline" to={settingsPath("account/machines")}>Machines</Link>
        <Button variant="ghost" size="sm" onClick={dismiss}>Dismiss</Button>
      </div>
    </section>
  );
}

const JOB_COUNTERS: [string, number][] = [["Code nodes", 2], ["Memories", 3], ["Items", 4]];

function FirstJobBlock({ mark, steps }: { mark: { status: string; reason?: string }; steps: SetupStep[] }) {
  const values = JOB_COUNTERS.map(([, n]) => countFrom(steps.find((s) => s.step === n)));
  return (
    <div data-testid="setup-status-first-job" data-state={mark.status} className="mt-3">
      <p className="text-[13px] font-semibold">
        ④ First job — {mark.status === "unknown" ? `unknown (${mark.reason ?? "could not be checked"})` : "pending"}
      </p>
      <dl className="mt-2 grid grid-cols-3 gap-2">
        {JOB_COUNTERS.map(([label], i) => (
          <div key={label} className="rounded-[10px] border border-line bg-surface-2 p-2.5">
            <dt className="font-mono text-[10px] uppercase tracking-wide text-faint">{label}</dt>
            <dd data-testid={`setup-status-count-${label.toLowerCase().replace(" ", "-")}`} className="text-title font-semibold tabular-nums text-fg">
              {values[i] === null ? <span title="could not be counted">?</span> : values[i]}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function MachineReportRow({ row }: { row: SetupMachineReport }) {
  const tone = row.state === "failed" ? "text-danger" : row.state === "clean" ? "text-accent" : "text-muted";
  const when = Number.isFinite(Date.parse(row.at)) ? relTime(row.at) : row.at;
  const what = row.state === "failed" ? `${row.fails} FAIL` : row.state === "unknown" ? `${row.unknowns || "an"} UNKNOWN` : "no failures";
  return (
    <li data-testid={row.state === "clean" ? "setup-status-clean" : "setup-status-failed"} data-machine={row.machine}>
      <div className="flex items-center gap-2">
        <p className={`text-[13px] ${tone}`}>
          {row.machine}: last report {when} had {what}
        </p>
        <MachineState machine={{ state: row.state, fail_count: row.fails, unknowns: row.unknowns }} />
      </div>
      {row.failing.length ? (
        <ul className={`mt-1 space-y-0.5 font-mono text-[12px] ${tone}`}>
          {row.failing.map((l, i) => (
            <li key={i}>{l.status} {l.name}{l.detail ? ` — ${l.detail}` : ""}</li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}
