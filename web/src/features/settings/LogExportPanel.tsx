/**
 * This box → Log export (PRD-47 S15 / GRPH-966).
 *
 * One collector for the whole deployment, which is why this panel is here and not under "This
 * project": one endpoint, one exporter, one queue. A per-project panel would be N editors for
 * a single shared thing — the mismatch GRPH-625 already fixed for credentials.
 *
 * The state floor does most of the work in this file. Every number on the strip is a count that
 * looks reassuring when it is actually unknown, so:
 *
 * - a failed FETCH is `PlannerError`, never a panel showing "off" — off and unknown are
 *   different states and this repo ships the wrong one by default;
 * - a `null` counter is an em dash with the reason in the caption below the strip, never `0`;
 * - `queue depth` is null whenever no exporter is running, because a backlog nobody is
 *   draining reads as an empty queue;
 * - Send test batch starts at NOT RUN. A probe that has not happened must not look like one
 *   that passed, and a failure names itself (`no_port` gets its own sentence, not "error").
 */
import * as React from "react";

import { CardGridSkeleton, FETCH_FAILED, PlannerError } from "@/components/planner/PlannerStates";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import {
  useLogExport,
  useLogExportSample,
  useLogExportTestBatch,
  useUpdateLogExport,
} from "@/lib/queries";
import type { LogExportConfig, LogExportStatus, LogExportTestResult, LogExportView } from "@/lib/types";

/** What "not run yet" says. Exported so the test can pin the wording rather than the layout. */
export const TEST_NOT_RUN = "Not run yet. Nothing has been sent — that is not a pass.";
export const UNKNOWN = "—";

/** The three signals and the OTLP kind each becomes. The mapping is the design's own. */
export const SIGNALS = [
  ["send_events", "Activity events", "as logs"],
  ["send_tool_calls", "MCP tool calls", "as traces"],
  ["send_heartbeats", "Agent heartbeats", "as metrics"],
] as const;

export const REDACTIONS = [
  ["redact_summaries", "Drop free-text summaries", "Titles, descriptions, reasons and notes an agent or a person wrote."],
  ["redact_client_ips", "Drop client IPs", "The address is removed from the record, not replaced with a placeholder."],
  ["mask_api_keys", "Mask API keys", "A collector still learns an agent acted; it does not learn which credential."],
] as const;

const STATE_LABEL: Record<LogExportStatus["state"], string> = {
  exporting: "Exporting",
  paused: "Paused",
  not_running: "On, but nothing is draining",
  unknown: "Unknown",
};

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("rounded-[13px] border border-line-2 bg-surface-2 p-4", className)}>
      {children}
    </div>
  );
}

function Toggle({
  checked, onChange, disabled, label, hint,
}: {
  checked: boolean; onChange: (next: boolean) => void; disabled: boolean;
  label: string; hint?: string;
}) {
  return (
    <label className="flex items-start gap-2 text-[12.5px] text-fg-2">
      <input
        type="checkbox"
        className="mt-0.5 accent-accent"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>
        <span className="font-medium text-fg-2">{label}</span>
        {hint ? <span className="ml-1.5 text-faint">— {hint}</span> : null}
      </span>
    </label>
  );
}

/** The strip's cell caption. Shared by all four cells so they cannot drift apart — `LastBatch`
 *  renders three of its own shapes and each has to line up with the numeric ones beside it. */
function StripLabel({ children }: { children: React.ReactNode }) {
  return <div className="font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

/** One strip cell. `null` is unknown and renders an em dash — never a zero. */
function Metric({ label, value, tone }: { label: string; value: number | null; tone?: string }) {
  return (
    <div className="min-w-0">
      <StripLabel>{label}</StripLabel>
      <div className={cn("mt-0.5 font-mono text-[16px]", value === null ? "text-muted" : (tone ?? "text-fg"))}>
        {value === null ? UNKNOWN : value.toLocaleString()}
      </div>
    </div>
  );
}

function LastBatch({ status }: { status: LogExportStatus }) {
  if (status.last_batch_state === "unavailable") {
    return <Metric label="Last batch" value={null} />;
  }
  if (status.last_batch_state === "never" || !status.last_batch) {
    return (
      <div className="min-w-0">
        <StripLabel>Last batch</StripLabel>
        <div className="mt-0.5 text-[13px] text-muted">never</div>
      </div>
    );
  }
  const b = status.last_batch;
  const when = b.ts ? new Date(b.ts).toLocaleString() : "";
  return (
    <div className="min-w-0">
      <StripLabel>Last batch</StripLabel>
      <div className={cn("mt-0.5 text-[13px]", b.ok ? "text-fg" : "text-st-blocked")}>
        {b.ok ? `${b.sent} sent` : b.error || "failed"}
        {b.kind === "test" ? <span className="ml-1.5 text-faint">test batch</span> : null}
      </div>
      {when ? <div className="mt-0.5 text-[11px] text-faint">{when}</div> : null}
    </div>
  );
}

function StatusStrip({ status }: { status: LogExportStatus }) {
  return (
    <div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <LastBatch status={status} />
        <Metric label="Sent 24h" value={status.sent_24h} />
        <Metric
          label="Dropped 24h"
          value={status.dropped_24h}
          tone={status.dropped_24h ? "text-st-blocked" : "text-fg"}
        />
        <Metric label="Queue depth" value={status.queue_depth} />
      </div>
      {status.coverage === "unavailable" && status.note ? (
        <p role="status" className="mt-3 text-[12px] text-st-review">{status.note}</p>
      ) : null}
      {status.queue_state === "exporter_not_running" ? (
        <p className="mt-3 text-[12px] text-muted">
          Queue depth is unknown, not zero: no exporter is running in this process, so a number
          here would be a backlog nobody is working down.
        </p>
      ) : null}
    </div>
  );
}

function TestResult({ result }: { result: LogExportTestResult }) {
  if (result.ok) {
    return (
      <p role="status" className="mt-2 text-[12.5px] text-fg-2">
        <span className="font-medium text-accent">Accepted.</span> {result.detail}
      </p>
    );
  }
  return (
    <p role="alert" className="mt-2 text-[12.5px] text-st-blocked">
      <span className="font-mono text-[11.5px]">{result.error}</span>
      <span className="mx-1.5 text-faint">·</span>
      {result.detail}
    </p>
  );
}

type Draft = {
  enabled: boolean;
  endpoint: string;
  protocol: string;
  compression: string;
  headers: { name: string; value: string }[];
  send_events: boolean;
  send_tool_calls: boolean;
  send_heartbeats: boolean;
  event_types: string[];
  redact_summaries: boolean;
  redact_client_ips: boolean;
  mask_api_keys: boolean;
};

function fromConfig(config: LogExportConfig): Draft {
  return {
    enabled: config.enabled,
    endpoint: config.endpoint,
    protocol: config.protocol,
    compression: config.compression,
    headers: config.headers.map((h) => ({ ...h })),
    send_events: config.send_events,
    send_tool_calls: config.send_tool_calls,
    send_heartbeats: config.send_heartbeats,
    event_types: [...config.event_types],
    redact_summaries: config.redact_summaries,
    redact_client_ips: config.redact_client_ips,
    mask_api_keys: config.mask_api_keys,
  };
}

const selectClass =
  "h-9 w-full rounded-[9px] border border-control bg-surface-2 px-3 text-[13px] text-fg outline-none disabled:cursor-not-allowed";

export function LogExportPanel() {
  const { data, isError, isPending, refetch } = useLogExport();

  if (isPending && !data) {
    // The shared skeleton rather than a hand-rolled pulse: the panel is a stack of cards, and
    // `PlannerStates` already has that shape with its own `aria-busy` contract.
    return (
      <div className="max-w-2xl">
        <div className="mb-3 h-4 w-40 animate-pulse rounded-md bg-surface-3" />
        <CardGridSkeleton cards={4} />
      </div>
    );
  }
  if (isError && !data) {
    // NOT a panel showing "off". A failed read has no idea whether export is running, and the
    // reassuring default is the wrong one.
    return (
      <PlannerError
        message={`${FETCH_FAILED} Whether log export is on was not read — this is not "off", and the counters are not zero.`}
        onRetry={() => { void refetch(); }}
      />
    );
  }
  if (!data) return null;
  // Keyed on the config's own stamp, and there is deliberately NO effect syncing the draft from
  // `data.config`: Send test batch invalidates this query so the strip can pick up the probe's
  // batch row, and an effect would reset the form on that refetch — wiping an endpoint the
  // operator was mid-typing on, which is the moment they are most likely to press the button.
  // A save moves `updated_at`, so a save remounts and a refetch does not.
  return <LogExportForm key={data.config.updated_at ?? "none"} view={data} />;
}

function LogExportForm({ view }: { view: LogExportView }) {
  const [draft, setDraft] = React.useState<Draft>(() => fromConfig(view.config));
  const [error, setError] = React.useState("");
  const [saved, setSaved] = React.useState(false);
  const [note, setNote] = React.useState("");
  const [test, setTest] = React.useState<LogExportTestResult | null>(null);
  const save = useUpdateLogExport();
  const probe = useLogExportTestBatch();
  const sampleQ = useLogExportSample({
    redact_summaries: draft.redact_summaries,
    redact_client_ips: draft.redact_client_ips,
    mask_api_keys: draft.mask_api_keys,
  });

  const writable = view.writable;
  const status = view.status;
  const sample = sampleQ.data?.sample ?? view.sample;

  function set<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  /** The header rows as the API wants them. Blank rows are dropped rather than sent as an
   *  empty-named header, which the server would then refuse or store as noise. */
  function headerMap() {
    return Object.fromEntries(
      draft.headers.filter((h) => h.name.trim()).map((h) => [h.name.trim(), h.value]),
    );
  }

  function onSave() {
    setError("");
    setNote("");
    save.mutate(
      { ...draft, headers: headerMap() },
      {
        onSuccess: (data) => {
          setSaved(true);
          setTimeout(() => setSaved(false), 1500);
          if (data.notes?.catch_up_note) setNote(data.notes.catch_up_note);
        },
        onError: (err) => setError(errorDetail(err, "Could not save log export.")),
      },
    );
  }

  function onTest() {
    setError("");
    probe.mutate(
      {
        endpoint: draft.endpoint,
        protocol: draft.protocol,
        compression: draft.compression,
        headers: headerMap(),
      },
      {
        onSuccess: (result) => setTest(result),
        // A probe that could not even be POSTed is still a failure to report, not a silence:
        // leaving `test` null would put the panel back to "not run", which reads as untried.
        onError: (err) => setTest({
          ok: false, ran: false, error: "request_failed",
          detail: errorDetail(err, "The test batch could not be sent."),
          records: null, latency_ms: null, endpoint: draft.endpoint,
        }),
      },
    );
  }

  const attrEntries = Object.entries(sample.attributes);

  return (
    <div className="max-w-2xl space-y-5">
      <div>
        <h2 className="text-[15px] font-semibold tracking-tight">Log export</h2>
        <p className="mt-1 max-w-[62ch] text-[12.5px] leading-relaxed text-muted">
          Where this box hands its telemetry to an OTLP collector. One collector for the whole
          deployment — not per project — so a record names its project instead.
        </p>
      </div>

      {/* On / off and the strip */}
      <Card>
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <Label>Status</Label>
            <div
              className={cn(
                "text-[14px] font-semibold",
                status.state === "exporting" && "text-accent",
                status.state === "not_running" && "text-st-review",
                status.state === "unknown" && "text-st-review",
              )}
            >
              {STATE_LABEL[status.state]}
            </div>
          </div>
          <Toggle
            checked={draft.enabled}
            disabled={!writable}
            onChange={(next) => set("enabled", next)}
            label={draft.enabled ? "Export on" : "Export off"}
          />
        </div>
        <StatusStrip status={status} />
        {status.state_note ? (
          <p className="mt-3 text-[12px] text-muted">{status.state_note}</p>
        ) : null}
        <p className="mt-3 border-t border-line pt-3 text-[12px] leading-relaxed text-muted">
          {view.retention_note}
        </p>
      </Card>

      {/* Collector */}
      <Card>
        <Label>Collector</Label>
        <div className="space-y-3">
          <div>
            <label className="mb-1.5 block text-[11.5px] text-muted" htmlFor="le-endpoint">
              Endpoint
            </label>
            <Input
              id="le-endpoint"
              aria-label="Collector endpoint"
              className="font-mono text-[12.5px]"
              placeholder="http://localhost:4318"
              value={draft.endpoint}
              disabled={!writable}
              aria-invalid={Boolean(view.endpoint_problem) || undefined}
              onChange={(e) => set("endpoint", e.target.value)}
            />
            <p className="mt-1.5 text-[11px] text-faint">
              The collector base, as OTEL_EXPORTER_OTLP_ENDPOINT defines it. Records go to
              /v1/logs, /v1/traces and /v1/metrics under it — so it needs a port.
            </p>
            {view.endpoint_problem ? (
              <p role="alert" className="mt-1.5 text-[12px] text-st-blocked">
                <span className="font-mono text-[11.5px]">{view.endpoint_problem.error}</span>
                <span className="mx-1.5 text-faint">·</span>
                {view.endpoint_problem.detail}
              </p>
            ) : null}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1.5 block text-[11.5px] text-muted" htmlFor="le-protocol">
                Protocol
              </label>
              <select
                id="le-protocol"
                aria-label="Protocol"
                className={selectClass}
                value={draft.protocol}
                disabled={!writable}
                onChange={(e) => set("protocol", e.target.value)}
              >
                {view.protocols.map((p) => (
                  <option key={p.id} value={p.id} disabled={!p.supported}>
                    {p.label}
                    {p.supported ? "" : " — not in this build"}
                  </option>
                ))}
              </select>
              <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
                {(view.protocols.find((p) => p.id === draft.protocol)?.note ?? "")}
              </p>
            </div>
            <div>
              <label className="mb-1.5 block text-[11.5px] text-muted" htmlFor="le-compression">
                Compression
              </label>
              <select
                id="le-compression"
                aria-label="Compression"
                className={selectClass}
                value={draft.compression}
                disabled={!writable}
                onChange={(e) => set("compression", e.target.value)}
              >
                {view.compressions.map((c) => (
                  <option key={c} value={c}>{c === "none" ? "None" : c}</option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <Label>Headers</Label>
            <div className="space-y-2">
              {draft.headers.map((h, i) => (
                <div key={i} className="flex items-center gap-2">
                  <Input
                    aria-label={`Header ${i + 1} name`}
                    className="font-mono text-[12px]"
                    placeholder="Authorization"
                    value={h.name}
                    disabled={!writable}
                    onChange={(e) => {
                      const headers = [...draft.headers];
                      headers[i] = { ...h, name: e.target.value };
                      set("headers", headers);
                    }}
                  />
                  <Input
                    aria-label={`Header ${i + 1} value`}
                    className="font-mono text-[12px]"
                    placeholder="Bearer …"
                    value={h.value}
                    disabled={!writable}
                    onChange={(e) => {
                      const headers = [...draft.headers];
                      headers[i] = { ...h, value: e.target.value };
                      set("headers", headers);
                    }}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove header ${i + 1}`}
                    disabled={!writable}
                    onClick={() => set("headers", draft.headers.filter((_x, j) => j !== i))}
                  >
                    ×
                  </Button>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={!writable}
                onClick={() => set("headers", [...draft.headers, { name: "", value: "" }])}
              >
                Add header
              </Button>
              <p className="text-[11px] text-faint">
                Values are stored as typed and shown masked. Saving a masked value back keeps
                the stored one; removing a row deletes the header.
              </p>
            </div>
          </div>

          <div className="border-t border-line pt-3">
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" variant="outline" size="sm" disabled={!writable || probe.isPending} onClick={onTest}>
                {probe.isPending ? "Sending…" : "Send test batch"}
              </Button>
            </div>
            {/* Three states, and the first one is the point: an unrun probe is not a pass. */}
            {test === null && !probe.isPending ? (
              <p className="mt-2 text-[12.5px] text-muted">{TEST_NOT_RUN}</p>
            ) : null}
            {test ? <TestResult result={test} /> : null}
          </div>
        </div>
      </Card>

      {/* What to send */}
      <Card>
        <Label>What to send</Label>
        <div className="space-y-2.5">
          {SIGNALS.map(([key, label, kind]) => (
            <Toggle
              key={key}
              checked={draft[key]}
              disabled={!writable}
              onChange={(next) => set(key, next)}
              label={label}
              hint={kind}
            />
          ))}
        </div>

        <div className="mt-4 border-t border-line pt-3">
          <Label>Event types</Label>
          {draft.send_events ? (
            draft.event_types.length === 0 ? (
              <p className="text-[12px] text-faint">
                Every action the ledger records. Tick one to narrow it — an empty list means all,
                not none.
              </p>
            ) : null
          ) : (
            <p className="text-[12px] text-faint">Events are off, so this filter has nothing to narrow.</p>
          )}
          {draft.send_events && view.event_type_options.length > 0 ? (
            <div className="mt-2 flex max-h-44 flex-wrap gap-1.5 overflow-y-auto">
              {view.event_type_options.map((action) => {
                const on = draft.event_types.includes(action);
                return (
                  <button
                    key={action}
                    type="button"
                    disabled={!writable}
                    aria-pressed={on}
                    className={cn(
                      "rounded-md border px-2 py-0.5 font-mono text-[11px] disabled:pointer-events-none disabled:opacity-50",
                      on
                        ? "border-accent/40 bg-accent/10 text-fg"
                        : "border-control text-muted hover:text-fg-2",
                    )}
                    onClick={() =>
                      set("event_types", on
                        ? draft.event_types.filter((a) => a !== action)
                        : [...draft.event_types, action])
                    }
                  >
                    {action}
                  </button>
                );
              })}
            </div>
          ) : null}
          {draft.send_events && view.event_type_options.length === 0 ? (
            <p className="text-[12px] text-faint">
              This ledger has recorded no actions yet, so there is nothing to filter on.
            </p>
          ) : null}
          {draft.event_types.length > 0 ? (
            <button
              type="button"
              className="mt-2 text-[11.5px] text-accent underline-offset-2 hover:underline disabled:pointer-events-none"
              disabled={!writable}
              onClick={() => set("event_types", [])}
            >
              Clear filter — send every action
            </button>
          ) : null}
        </div>
      </Card>

      {/* Redaction and the live sample */}
      <Card>
        <Label>Redaction</Label>
        <div className="space-y-2.5">
          {REDACTIONS.map(([key, label, hint]) => (
            <Toggle
              key={key}
              checked={draft[key]}
              disabled={!writable}
              onChange={(next) => set(key, next)}
              label={label}
              hint={hint}
            />
          ))}
        </div>
        <p className="mt-3 border-t border-line pt-3 text-[11.5px] leading-relaxed text-faint">
          Secrets, home paths and addresses inside free text are redacted whatever these say — a
          collector is off-box. These three decide what else goes.
        </p>

        <div className="mt-4">
          <Label>Sample record</Label>
          {sampleQ.isError ? (
            <p className="text-[12px] text-st-blocked">
              {FETCH_FAILED} The sample was not read, so this panel is not showing what would be
              sent.
            </p>
          ) : (
            <div className={cn("transition-opacity", sampleQ.isPlaceholderData && "opacity-60")}>
              {sample.source === "synthetic" && sample.note ? (
                <p className="mb-2 text-[11.5px] leading-relaxed text-st-review">
                  {sample.note}
                </p>
              ) : null}
              <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-[10.5px] text-faint">
                <span>{sample.kind}</span>
                <span>{sample.severity}</span>
                <span className="text-fg-2">{sample.body}</span>
                <span>{sample.timestamp}</span>
              </div>
              <dl className="overflow-x-auto rounded-md border border-line-2 bg-surface px-2.5 py-2">
                {attrEntries.map(([k, v]) => (
                  <div key={k} className="flex gap-2 font-mono text-[11px] leading-relaxed">
                    <dt className="flex-none text-muted">{k}</dt>
                    <dd className="min-w-0 break-all text-fg-2">{String(v)}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>
      </Card>

      {note ? <p className="text-[12px] text-st-review">{note}</p> : null}
      {error ? <p className="text-[12px] text-st-blocked">{error}</p> : null}

      <Button size="sm" onClick={onSave} disabled={!writable || save.isPending}>
        {saved ? "Saved" : "Save log export"}
      </Button>
    </div>
  );
}
