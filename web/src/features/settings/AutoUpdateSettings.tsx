/**
 * Updates → Auto-update (GRPH-1299, redrawn in GRPH-1302). Opt-in, off by default. One
 * "Upgrade window" (weekdays, start/end) in the chosen time zone; saved as both the check and
 * the install window. The zone stays a control: the API container has no TZ, so the server's
 * own clock is UTC, not the owner's. Until a zone is saved the browser's is pre-selected. Unavailable (and says why) on hosted boxes and boxes with no apply path.
 * A failed auto-install is shown with its error; `progressError` is the install log's own
 * error line (GRPH-1298). `progress` is the live install log, for the "installing" box.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAutoUpdate, useUpdateAutoUpdate } from "@/lib/queries";
import type { AutoUpdateStatus, AutoUpdateWindow, UpdateProgress } from "@/lib/types";

/**
 * One bar segment per install step: done = accent, active = half, failed = danger, pending =
 * line. `openEnded` (a native install: no known total) adds a dotted tail, not invented steps.
 */
export function StepBar({ steps, openEnded = false }: { steps: UpdateProgress["steps"]; openEnded?: boolean }) {
  return (
    <div className="flex items-center gap-1" aria-hidden>
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
      {openEnded ? (
        <span data-testid="step-tail" className="h-0 flex-1 border-t-2 border-dotted border-line" />
      ) : null}
    </div>
  );
}

/** "done / total"; a native install's log has no known total, so it says so (GRPH-1302). */
export function stepCount(steps: UpdateProgress["steps"], via?: string): string {
  const done = steps.filter((x) => x.status === "done").length;
  return via === "native" ? `${done} done · total not known` : `${done} / ${steps.length}`;
}

/** The browser's IANA zone; an object so tests can stub it. */
export const browserZone = {
  get: (): string => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch {
      return "UTC";
    }
  },
};

const zoneSelectClass =
  "h-9 w-64 rounded-(--radius-control) border border-control bg-surface-2 px-3 text-[13px] font-normal text-fg outline-none disabled:cursor-not-allowed";

/** IANA zones from the browser, or null where Intl.supportedValuesOf is missing (text-input fallback). */
function zoneList(): string[] | null {
  const f = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf;
  if (typeof f !== "function") return null;
  try {
    return f("timeZone");
  } catch {
    return null;
  }
}

const DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

interface Draft {
  enabled: boolean;
  timezone: string;
  window: AutoUpdateWindow;
}

function toDraft(s: AutoUpdateStatus): Draft {
  // One window in the UI. When the stored windows differ, the install window is the one shown.
  // A never-saved zone is a draft of the browser's; nothing is written until Save.
  return {
    enabled: s.enabled,
    timezone: s.timezone_set ? s.timezone : browserZone.get(),
    window: { ...s.install },
  };
}

function ZonePicker({ value, stored, storedSet, disabled, clock, onChange }: {
  value: string; stored: string; storedSet: boolean; disabled: boolean; clock: string;
  onChange: (tz: string) => void;
}) {
  const zones = React.useMemo(zoneList, []);
  const browser = browserZone.get();
  // Some ICU builds leave "UTC" (and a stored legacy alias) out of supportedValuesOf; keep them selectable.
  const options = React.useMemo(() => {
    if (!zones) return null;
    const extra = ["UTC", value, stored].filter((z) => z && !zones.includes(z));
    return [...new Set(extra), ...zones];
  }, [zones, value, stored]);
  return (
    <div className="mt-3">
      <label className="flex flex-col items-start gap-1 text-[12px] font-semibold text-fg">
        Timezone
        {options ? (
          <select
            aria-label="Timezone"
            className={zoneSelectClass}
            value={value}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
          >
            {options.map((z) => <option key={z} value={z}>{z}</option>)}
          </select>
        ) : (
          <Input
            aria-label="Timezone"
            className="w-64 font-normal"
            value={value}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
      </label>
      <p className="mt-1 text-[12px] text-muted">
        {!storedSet && value === browser ? "Picked from this browser." : ""}
        {storedSet && stored !== browser ? (
          <>
            Your browser is in {browser}.
            {value !== browser ? (
              <>
                {" "}
                <button type="button" className="text-accent underline-offset-2 hover:underline"
                  disabled={disabled} onClick={() => onChange(browser)}>
                  Use it
                </button>
              </>
            ) : null}
          </>
        ) : null}
      </p>
      <p className="mt-1 text-[12px] text-muted">Box time is {clock}. The window below runs on this clock.</p>
    </div>
  );
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
    <fieldset className="mt-3 rounded-[10px] border border-line p-3" disabled={disabled}>
      <legend className="px-1 font-mono text-[10px] uppercase tracking-wide text-faint">Upgrade window</legend>
      <div className="space-y-3">
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
                  on ? "border-accent bg-accent text-bg" : "border-control bg-surface text-fg-2"}`}
              >
                {d}
              </button>
            );
          })}
        </div>
        <div className="flex gap-3">
          <label className="flex flex-col gap-1 text-[12px] text-fg-2">
            Start
            <Input aria-label="Upgrade window start" type="time" className="w-28" value={value.start}
              onChange={(e) => onChange({ ...value, start: e.target.value })} />
          </label>
          <label className="flex flex-col gap-1 text-[12px] text-fg-2">
            End
            <Input aria-label="Upgrade window end" type="time" className="w-28" value={value.end}
              onChange={(e) => onChange({ ...value, end: e.target.value })} />
          </label>
        </div>
      </div>
    </fieldset>
  );
}

/** "HH:MM <IANA zone>" for `iso` in `tz`; in `fallback` while `tz` is not a zone (fallback input mid-typing). */
function hostClock(iso: string, tz: string, fallback = "UTC"): string {
  try {
    new Intl.DateTimeFormat("en-GB", { timeZone: tz });
  } catch {
    tz = fallback;
  }
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz,
    }).formatToParts(new Date(iso));
    const g = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
    return `${g("hour")}:${g("minute")} ${tz}`;
  } catch {
    return `${iso} ${tz}`;
  }
}

export function AutoUpdateSettings({ progressError = "", progress = null, via }: {
  progressError?: string; progress?: UpdateProgress | null; via?: string;
}) {
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
        timezone: draft!.timezone.trim(),
        check_days: w.days, check_start: w.start, check_end: w.end,
        install_days: w.days, install_start: w.start, install_end: w.end,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save.");
    }
  }

  const on = draft.enabled && !locked;
  const hasBody = locked || data.enabled || on || !!data.last_auto_install_tag;
  const installing = data.last_auto_install_outcome === "started" && !!data.last_auto_install_tag
    && progress?.state === "running" && progress.tag === data.last_auto_install_tag;
  const failureText = progressError || data.last_auto_install_error;
  const failureDetail = failureText ? `: ${failureText}${/[.!?]$/.test(failureText) ? "" : "."}` : ".";
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
            <span className="font-mono text-[12px] font-normal text-fg-2">{stepCount(progress.steps, via)}</span>
          </div>
          <div className="mt-2"><StepBar steps={progress.steps} openEnded={via === "native"} /></div>
          <p className="mt-2 text-[12px] text-muted">
            Started at {hostClock(data.last_auto_install_at ?? data.host_now, data.timezone)}.
            {" "}Step detail is in Status above.
          </p>
        </div>
      ) : null}
      {failed ? (
        <p role="alert" className="mb-3 rounded-md border border-danger p-2 text-[13px] font-semibold text-danger">
          The last automatic update to {data.last_auto_install_tag} failed
          {failureDetail}
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
      {hasBody ? (
        <div className="mt-3 space-y-2 border-t border-line pt-3">
      {data.last_auto_install_tag ? (
        <p className="text-[12px] text-fg-2">
          Last auto-update: <span className="font-mono">{data.last_auto_install_tag}</span>{" "}
          {data.last_auto_install_outcome || "started"} at {when(data.last_auto_install_at, data.timezone)}
        </p>
      ) : null}
      {data.last_auto_install_outcome === "skipped" && data.last_auto_install_error ? (
        <p className="mt-1 text-[12px] text-fg-2">Skipped: {data.last_auto_install_error}</p>
      ) : null}
      {locked ? (
        <p className="text-[12px] text-muted">Auto-update is unavailable: {data.unavailable_reason}.</p>
      ) : null}
      {data.enabled ? (
        <dl className="mt-3 space-y-0.5 text-[12px] text-fg-2">
          <div>Next check at {when(data.next_check_at, data.timezone)}</div>
          <div>Last checked {when(data.last_check_at, data.timezone)}
            {data.last_check_state ? ` — ${data.last_check_state}` : ""}</div>
        </dl>
      ) : null}
      {on || (data.enabled && !locked) ? (
        <>
          <ZonePicker value={draft.timezone} stored={data.timezone} storedSet={data.timezone_set}
            disabled={locked} clock={hostClock(data.host_now, draft.timezone, data.timezone)}
            onChange={(timezone) => setDraft({ ...draft, timezone })} />
          <WindowEditor value={draft.window} disabled={locked}
            onChange={(window) => setDraft({ ...draft, window })} />
          <div className="mt-3 flex items-center gap-2">
            <Button type="button" disabled={locked || save.isPending} onClick={() => { void onSave(); }}>
              {save.isPending ? "Saving…" : "Save auto-update"}
            </Button>
            {error ? <span role="alert" className="text-[12px] text-danger">{error}</span> : null}
          </div>
        </>
      ) : null}
        </div>
      ) : null}
    </div>
  );
}
