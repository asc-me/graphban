/**
 * Updates → Auto-update (GRPH-1299). Opt-in, off by default. A check window and an install
 * window, each with weekdays and a start/end time in the chosen timezone. Unavailable (and
 * says why) on hosted boxes and boxes with no apply path. A failed auto-install is shown
 * with its error; `progressError` is the install log's own error line (GRPH-1298).
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAutoUpdate, useUpdateAutoUpdate } from "@/lib/queries";
import type { AutoUpdateStatus, AutoUpdateWindow } from "@/lib/types";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

interface Draft {
  enabled: boolean;
  timezone: string;
  check: AutoUpdateWindow;
  install: AutoUpdateWindow;
}

function toDraft(s: AutoUpdateStatus): Draft {
  return { enabled: s.enabled, timezone: s.timezone, check: { ...s.check }, install: { ...s.install } };
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
  label, value, disabled, onChange,
}: { label: string; value: AutoUpdateWindow; disabled: boolean; onChange: (w: AutoUpdateWindow) => void }) {
  return (
    <fieldset className="mt-3" disabled={disabled}>
      <legend className="mb-1 text-[12px] font-semibold text-fg">{label}</legend>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex gap-2" role="group" aria-label={`${label} days`}>
          {DAYS.map((d, i) => (
            <label key={d} className="flex items-center gap-1 text-[12px] text-fg-2">
              <input
                type="checkbox"
                checked={value.days.includes(i)}
                onChange={(e) => onChange({
                  ...value,
                  days: e.target.checked ? [...value.days, i].sort() : value.days.filter((x) => x !== i),
                })}
              />
              {d}
            </label>
          ))}
        </div>
        <label className="flex items-center gap-1 text-[12px] text-fg-2">
          From
          <Input aria-label={`${label} start`} type="time" className="w-28" value={value.start}
            onChange={(e) => onChange({ ...value, start: e.target.value })} />
        </label>
        <label className="flex items-center gap-1 text-[12px] text-fg-2">
          to
          <Input aria-label={`${label} end`} type="time" className="w-28" value={value.end}
            onChange={(e) => onChange({ ...value, end: e.target.value })} />
        </label>
      </div>
    </fieldset>
  );
}

export function AutoUpdateSettings({ progressError = "" }: { progressError?: string }) {
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
    try {
      await save.mutateAsync({
        enabled: draft!.enabled,
        timezone: draft!.timezone,
        check_days: draft!.check.days,
        check_start: draft!.check.start,
        check_end: draft!.check.end,
        install_days: draft!.install.days,
        install_start: draft!.install.start,
        install_end: draft!.install.end,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save.");
    }
  }

  return (
    <div className="mt-4 rounded-[13px] border border-line-2 bg-surface-2 p-4">
      <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">Auto-update</div>
      <label className="flex items-center gap-2 text-[13px] text-fg">
        <input
          type="checkbox"
          checked={draft.enabled && !locked}
          disabled={locked}
          onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
        />
        Update this box automatically
      </label>
      {locked ? (
        <p className="mt-1 text-[12px] text-muted">Auto-update is unavailable: {data.unavailable_reason}.</p>
      ) : (
        <p className="mt-1 text-[12px] text-muted">Off by default. Checks and installs only inside the windows below.</p>
      )}
      <label className="mt-3 flex items-center gap-2 text-[12px] text-fg-2">
        Timezone
        <Input aria-label="Timezone" className="w-56" value={draft.timezone} disabled={locked}
          onChange={(e) => setDraft({ ...draft, timezone: e.target.value })} />
      </label>
      <WindowEditor label="Check window" value={draft.check} disabled={locked}
        onChange={(check) => setDraft({ ...draft, check })} />
      <WindowEditor label="Install window" value={draft.install} disabled={locked}
        onChange={(install) => setDraft({ ...draft, install })} />
      <div className="mt-3 flex items-center gap-2">
        <Button type="button" disabled={locked || save.isPending} onClick={() => { void onSave(); }}>
          {save.isPending ? "Saving…" : "Save auto-update"}
        </Button>
        {error ? <span role="alert" className="text-[12px] text-danger">{error}</span> : null}
      </div>
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
      {failed ? (
        <p role="alert" className="mt-2 rounded-md border border-danger p-2 text-[13px] font-semibold text-danger">
          The last automatic update to {data.last_auto_install_tag} failed
          {progressError || data.last_auto_install_error ? `: ${progressError || data.last_auto_install_error}` : "."}
          {" "}It will not be retried; install a newer release or use Install above.
        </p>
      ) : null}
    </div>
  );
}
