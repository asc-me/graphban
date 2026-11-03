/**
 * Updates → Auto-update (GRPH-1299, redrawn in GRPH-1302). Opt-in, off by default. One
 * "Upgrade window" (weekdays, start/end) on the host's clock; saved as both the check and the
 * install window. Unavailable (and says why) on hosted boxes and boxes with no apply path.
 * A failed auto-install is shown with its error; `progressError` is the install log's own
 * error line (GRPH-1298). `progress` is the live install log, for the "installing" box.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAutoUpdate, useUpdateAutoUpdate } from "@/lib/queries";
import type { AutoUpdateStatus, AutoUpdateWindow, UpdateProgress } from "@/lib/types";

/** One bar segment per install step: done = accent, active = half, failed = danger, pending = line. */
export function StepBar({ steps }: { steps: UpdateProgress["steps"] }) {
  return (
    <div className="flex gap-1" aria-hidden>
      {steps.map((st) => (
        <span
          key={st.key}
          data-testid="step-segment"
          data-status={st.status}
          className={`relative h-1.5 flex-1 overflow-hidden rounded-full ${
            st.status === "failed" ? "bg-danger" : st.status === "done" ? "bg-accent" : "bg-line"}`}
        >
          {st.status === "active" ? <span className="absolute inset-y-0 left-0 w-1/2 bg-accent" /> : null}
        </span>
      ))}
    </div>
  );
}

export function stepCount(steps: UpdateProgress["steps"]): string {
  return `${steps.filter((x) => x.status === "done").length} / ${steps.length}`;
}

const DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

interface Draft {
  enabled: boolean;
  window: AutoUpdateWindow;
}

function toDraft(s: AutoUpdateStatus): Draft {
  // One window in the UI. When the stored windows differ, the install window is the one shown.
  return { enabled: s.enabled, window: { ...s.install } };
}

function when(iso: string | null, tz: string): string {
  if (!iso) return "never";
  try {
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", timeZone: tz }).format(new Date(iso)) + ` (${tz})`;
  } catch {
    return new Date(iso).toISOString();
  }
}

function WindowEditor({
  value, disabled, onChange,
}: { value: AutoUpdateWindow; disabled: boolean; onChange: (w: AutoUpdateWindow) => void }) {
  return (
    <fieldset className="mt-3" disabled={disabled}>
      <legend className="mb-1 text-[12px] font-semibold text-fg">Upgrade window</legend>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex gap-1.5" role="group" aria-label="Upgrade window days">
          {DAYS.map((d, i) => {
            const on = value.days.includes(i);
            return (
              <button
                key={d}
                type="button"
                aria-pressed={on}
                aria-label={DAY_NAMES[i]}
                onClick={() => onChange({
                  ...value,
                  days: on ? value.days.filter((x) => x !== i) : [...value.days, i].sort(),
                })}
                className={`h-8 w-8 rounded-full border text-small ${
                  on ? "border-accent bg-accent text-white" : "border-control bg-surface text-fg-2"}`}
              >
                {d}
              </button>
            );
          })}
        </div>
        <label className="flex items-center gap-1 text-[12px] text-fg-2">
          Start
          <Input aria-label="Upgrade window start" type="time" className="w-28" value={value.start}
            onChange={(e) => onChange({ ...value, start: e.target.value })} />
        </label>
        <label className="flex items-center gap-1 text-[12px] text-fg-2">
          End
          <Input aria-label="Upgrade window end" type="time" className="w-28" value={value.end}
            onChange={(e) => onChange({ ...value, end: e.target.value })} />
        </label>
      </div>
    </fieldset>
  );
}

function hostClock(iso: string, tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz, timeZoneName: "short",
    }).formatToParts(new Date(iso));
    const g = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
    return `${g("hour")}:${g("minute")} ${g("timeZoneName")}`;
  } catch {
    return `${iso} ${tz}`;
  }
}

export function AutoUpdateSettings(
  { progressError = "", progress = null }: { progressError?: string; progress?: UpdateProgress | null },
) {
  const { data, isPending, isError } = useAutoUpdate();
  const save = useUpdateAutoUpdate();
  const [draft, setDraft] = React.useState<Draft | null>(null);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    if (data && draft === null) setDraft(toDraft(data));
  }, [data, draft]);

  if (isPending) return <p className="mt-4 text-[13px] text-muted">Loading auto-update settings…</p>;
  if (isError || !data || !draft) {
    return <p className="mt-4 text-[13px] text-fg-2">Could not read the auto-update settings.</p>;
  }
  const locked = !data.available;
  const failed = data.last_auto_install_outcome === "failed";

  async function onSave() {
    setError("");
    const w = draft!.window;
    try {
      await save.mutateAsync({
        enabled: draft!.enabled,
        timezone: data!.host_timezone,
        check_days: w.days, check_start: w.start, check_end: w.end,
        install_days: w.days, install_start: w.start, install_end: w.end,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save.");
    }
  }

  const on = draft.enabled && !locked;
  const installing = data.last_auto_install_outcome === "started" && !!data.last_auto_install_tag
    && progress?.state === "running" && progress.tag === data.last_auto_install_tag;
  const activeStep = progress?.steps.find((x) => x.status === "active");

  return (
    <div className="mt-4 rounded-[13px] border border-line-2 bg-surface-2 p-4">
      <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">Auto-update</div>
      {installing && progress ? (
        <div role="status" className="mb-3 rounded-md border border-accent p-3">
          <div className="flex items-center justify-between text-[13px] font-semibold text-fg">
            <span>
              Auto-update is installing {data.last_auto_install_tag}
              {activeStep ? ` — ${activeStep.label.toLowerCase()}` : ""}
            </span>
            <span className="font-mono text-[12px] font-normal text-fg-2">{stepCount(progress.steps)}</span>
          </div>
          <div className="mt-2"><StepBar steps={progress.steps} /></div>
          <p className="mt-2 text-[12px] text-muted">
            Started inside the upgrade window at {hostClock(data.last_auto_install_at ?? data.host_now, data.host_timezone)}.
            {" "}Step detail is in Status above.
          </p>
        </div>
      ) : null}
      {failed ? (
        <p role="alert" className="mb-3 rounded-md border border-danger p-2 text-[13px] font-semibold text-danger">
          The last automatic update to {data.last_auto_install_tag} failed
          {progressError || data.last_auto_install_error ? `: ${progressError || data.last_auto_install_error}` : "."}
          {" "}It will not be retried; install a newer release or use Install above.
        </p>
      ) : null}
      <div className="flex items-center justify-between gap-3">
        <div>
          <div id="auto-update-label" className="text-[13px] text-fg">Update this box automatically</div>
          <p className="mt-0.5 text-[12px] text-muted">Off by default. Checks and installs only inside the window below.</p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-labelledby="auto-update-label"
          disabled={locked}
          onClick={() => setDraft({ ...draft, enabled: !draft.enabled })}
          className={`relative h-5 w-9 shrink-0 rounded-full border border-control disabled:opacity-50 ${on ? "bg-accent" : "bg-line"}`}
        >
          <span className={`absolute top-0.5 h-3.5 w-3.5 rounded-full bg-surface transition-all ${on ? "left-[18px]" : "left-0.5"}`} />
        </button>
      </div>
      {locked ? (
        <p className="mt-3 border-t border-line-2 pt-2 text-[12px] text-muted">Auto-update is unavailable: {data.unavailable_reason}.</p>
      ) : null}
      {on || (data.enabled && !locked) ? (
        <>
          <p className="mt-3 text-[12px] text-muted">
            This deployment&apos;s host time is {hostClock(data.host_now, data.host_timezone)}. The window below uses this clock.
          </p>
          <WindowEditor value={draft.window} disabled={locked}
            onChange={(window) => setDraft({ ...draft, window })} />
        </>
      ) : null}
      {on || (data.enabled && !locked) ? (
        <div className="mt-3 flex items-center gap-2">
          <Button type="button" disabled={locked || save.isPending} onClick={() => { void onSave(); }}>
            {save.isPending ? "Saving…" : "Save auto-update"}
          </Button>
          {error ? <span role="alert" className="text-[12px] text-danger">{error}</span> : null}
        </div>
      ) : null}
      {data.enabled ? (
        <dl className="mt-3 space-y-0.5 text-[12px] text-fg-2">
          <div>Next check at {when(data.next_check_at, data.timezone)}</div>
          <div>Last checked {when(data.last_check_at, data.timezone)}
            {data.last_check_state ? ` — ${data.last_check_state}` : ""}</div>
        </dl>
      ) : null}
      {data.last_auto_install_tag ? (
        <p className="mt-1 text-[12px] text-fg-2">
          Last auto-update: <span className="font-mono">{data.last_auto_install_tag}</span>{" "}
          {data.last_auto_install_outcome || "started"} at {when(data.last_auto_install_at, data.timezone)}
        </p>
      ) : null}
      {data.last_auto_install_outcome === "skipped" && data.last_auto_install_error ? (
        <p className="mt-1 text-[12px] text-fg-2">Skipped: {data.last_auto_install_error}</p>
      ) : null}
    </div>
  );
}
